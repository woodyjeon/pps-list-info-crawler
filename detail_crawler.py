"""조달청 목록정보시스템 품목 상세페이지(productSearchView.do) 크롤러.

목록 크롤링 결과 CSV(combined_*.csv 등)의 상세URL을 읽어 각 품목 상세페이지를
수집한다. 공통속성정보 + 가변 개별속성정보를 Wide 포맷 CSV로 저장한다.

진행분은 JSONL 체크포인트로 즉시 보존하므로 중단 후 재실행 시 완료분은 건너뛴다(이어받기).

사용 예:
    python detail_crawler.py
    python detail_crawler.py --input output/combined_20260610_160536.csv --limit 20
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from crawler import BASE_URL, USER_AGENT, make_session  # noqa: F401 (USER_AGENT 재사용 가능성)

# 큰 CSV(상세URL 다수) 처리를 위해 필드 크기 제한 상향
csv.field_size_limit(10 * 1024 * 1024)

# 상세페이지 공통속성 -> 출력 키 매핑 (고정 컬럼)
COMMON_FIELDS = [
    "물품목록번호",
    "물품분류번호",
    "물품식별번호",
    "품명",
    "세부품명번호",
    "세부품명영문명",
    "단위",
    "내용연수",
    "상품원산지국가명",
    "품목구분",
    "부품여부",
    "품목등록일",
]

COMMON2_FIELDS = [
    "모델명",
    "상품브랜드명",
    "품목명",
    "제조업체명",
    "제품설명",
]

# Wide CSV 고정 컬럼 순서 (가변 개별속성 컬럼은 이 뒤에 정렬되어 붙음)
FIXED_COLUMNS = [
    "물품식별번호",
    "세부품명번호",
    "물품목록번호",
    "물품분류번호",
    "품명",
    "세부품명영문명",
    "단위",
    "내용연수",
    "상품원산지국가명",
    "품목구분",
    "부품여부",
    "품목등록일",
    "모델명",
    "상품브랜드명",
    "품목명",
    "제조업체명",
    "제품설명",
    "분류경로",
    "이미지URL",
    "상세URL",
]


def fetch_detail(session, url, max_retries=3, delay=0.5):
    """상세 페이지를 GET 하고 응답 HTML(str)을 반환한다."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=60)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"서버 오류 {resp.status_code}")
            resp.raise_for_status()
            resp.encoding = "UTF-8"
            return resp.text
        except (requests.RequestException, requests.HTTPError) as err:
            last_err = err
            backoff = delay * (2 ** (attempt - 1))
            print(
                f"    [재시도 {attempt}/{max_retries}] {url} 실패: {err} -> {backoff:.1f}s 대기",
                file=sys.stderr,
            )
            time.sleep(backoff)
    raise RuntimeError(f"{url} 요청을 {max_retries}회 시도했으나 실패: {last_err}")


def parse_breadcrumb(soup):
    """현재물품분류(분류경로)를 ' > ' 로 연결해 반환한다."""
    box = soup.select_one("div.regLocation ul")
    if box is None:
        return ""
    parts = []
    for li in box.find_all("li"):
        # 라벨 li 는 <i class="icon ..."> 아이콘을 포함 -> 건너뜀
        if li.find("i") is not None:
            continue
        text = li.get_text(strip=True)
        if text:
            parts.append(text)
    return " > ".join(parts)


def _find_table_by_caption(soup, caption_text):
    """caption 텍스트가 일치하는 table.tableType_ViewPop 을 반환한다."""
    for table in soup.find_all("table", class_="tableType_ViewPop"):
        cap = table.find("caption")
        if cap and cap.get_text(strip=True) == caption_text:
            return table
    return None


def parse_kv_table(soup, caption_text):
    """th(라벨) -> td(값) 형태의 테이블을 dict 로 파싱한다.

    공통속성정보 / 공통속성정보2 양쪽에 공용. th 가 있는 행만 사용하므로
    이미지(rowspan) 셀이 섞인 첫 행도 안전하게 처리된다.
    """
    table = _find_table_by_caption(soup, caption_text)
    result = {}
    if table is None:
        return result
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        th = tr.find("th")
        if th is None:
            continue
        key = th.get_text(strip=True)
        # th 다음의 첫 td 가 값
        td = th.find_next_sibling("td")
        if td is None:
            continue
        value = td.get_text(" ", strip=True)
        # '5611210201 (작업용의자)' 같은 nbsp 정리
        value = value.replace("\xa0", " ").strip()
        result[key] = value
    return result


def parse_individual(soup):
    """개별속성정보(가변)를 {속성명: '값 단위'} dict 로 반환한다."""
    table = _find_table_by_caption(soup, "개별속성정보")
    result = {}
    if table is None:
        return result
    body = table.find("tbody")
    if body is None:
        return result
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        name = tds[0].get_text(strip=True)
        value = tds[1].get_text(" ", strip=True)
        unit = tds[2].get_text(strip=True) if len(tds) >= 3 else ""
        if not name:
            continue
        combined = f"{value} {unit}".strip() if unit else value
        result[name] = combined
    return result


def parse_image_url(soup):
    """고화질 이미지 URL 을 data-file-full-path 에서 추출한다."""
    img = soup.select_one("img.common_img_tags")
    if img is None:
        return ""
    full = img.get("data-file-full-path", "")
    if full:
        return urljoin(BASE_URL, "/product/productImageViewTagPopup.do?fileFullPath=" + full)
    src = img.get("src", "")
    return urljoin(BASE_URL, src) if src else ""


def parse_detail(html, detail_url):
    """상세 페이지 HTML 을 파싱해 하나의 레코드(dict)로 반환한다.

    개별속성정보는 '_개별속성' 하위 dict 에 담는다.
    """
    soup = BeautifulSoup(html, "lxml")

    record = {"상세URL": detail_url}
    record["분류경로"] = parse_breadcrumb(soup)

    common = parse_kv_table(soup, "공통속성정보")
    for k in COMMON_FIELDS:
        record[k] = common.get(k, "")
    # 세부품명번호는 '5611210201 (작업용의자)' 형태 -> 앞 숫자만
    clsfc = record.get("세부품명번호", "")
    if clsfc:
        record["세부품명번호"] = clsfc.split("(")[0].strip()

    common2 = parse_kv_table(soup, "공통속성정보2")
    for k in COMMON2_FIELDS:
        record[k] = common2.get(k, "")

    record["이미지URL"] = parse_image_url(soup)
    record["_개별속성"] = parse_individual(soup)
    return record


def read_targets(csv_path, url_col):
    """기존 결과 CSV 에서 (상세URL, 물품식별번호) 목록을 읽는다."""
    targets = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or url_col not in reader.fieldnames:
            raise ValueError(
                f"입력 CSV 에 '{url_col}' 컬럼이 없습니다. (컬럼: {reader.fieldnames})"
            )
        for row in reader:
            url = (row.get(url_col) or "").strip()
            if not url:
                continue
            idntfc = (row.get("물품식별번호") or "").strip()
            if not idntfc:
                qs = parse_qs(urlparse(url).query)
                idntfc = qs.get("goodsIdntfcNo", [""])[0]
            targets.append((url, idntfc))
    return targets


def load_done(jsonl_path):
    """JSONL 체크포인트에서 이미 완료된 물품식별번호 집합을 읽는다."""
    done = set()
    if not jsonl_path.exists():
        return done
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = rec.get("물품식별번호") or rec.get("_key")
            if key:
                done.add(str(key))
    return done


def flatten_record(record):
    """parse_detail 결과를 평면 dict(개별속성 펼침)로 변환한다."""
    flat = {k: v for k, v in record.items() if k != "_개별속성"}
    for name, value in record.get("_개별속성", {}).items():
        flat[name] = value
    return flat


def build_wide_csv(jsonl_path, out_csv):
    """JSONL 전체를 읽어 속성명 합집합 컬럼으로 Wide CSV(UTF-8 BOM)를 작성한다."""
    rows = []
    attr_cols = []
    seen_attr = set()
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            flat = flatten_record(rec)
            rows.append(flat)
            for col in flat:
                if col not in FIXED_COLUMNS and col not in seen_attr:
                    seen_attr.add(col)
                    attr_cols.append(col)

    attr_cols.sort()
    columns = FIXED_COLUMNS + attr_cols

    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})

    return len(rows), len(attr_cols)


def find_latest_combined(out_dir):
    """output 폴더에서 가장 최근 combined_*.csv 경로를 반환한다."""
    files = sorted(Path(out_dir).glob("combined_*.csv"))
    return files[-1] if files else None


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="조달청 품목 상세페이지 크롤러")
    p.add_argument("--input", default=None,
                   help="상세URL이 담긴 입력 CSV (기본: output 폴더 최신 combined_*.csv)")
    p.add_argument("--url-column", default="상세URL", help="상세URL 컬럼명")
    p.add_argument("--out-dir", default="output", help="결과 저장 폴더")
    p.add_argument("--delay", type=float, default=0.5, help="요청 간 지연(초)")
    p.add_argument("--limit", type=int, default=0, help="수집할 최대 건수 (0=전체)")
    p.add_argument("--no-resume", action="store_true", help="이어받기 비활성화(처음부터 다시)")
    p.add_argument("--no-verify", action="store_true", help="SSL 인증서 검증 비활성화")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    input_path = Path(args.input) if args.input else find_latest_combined(out_dir)
    if input_path is None or not input_path.exists():
        print("입력 CSV 를 찾을 수 없습니다. --input 으로 지정하세요.", file=sys.stderr)
        return 1
    print(f"입력 CSV: {input_path}")

    targets = read_targets(input_path, args.url_column)
    if not targets:
        print("상세URL 이 없습니다. 입력 CSV 를 확인하세요.", file=sys.stderr)
        return 1
    if args.limit:
        targets = targets[: args.limit]
    print(f"수집 대상 {len(targets)}건")

    jsonl_path = out_dir / "detail_records.jsonl"
    done = set() if args.no_resume else load_done(jsonl_path)
    if done:
        print(f"이어받기: 이미 완료 {len(done)}건 -> 건너뜀")

    if args.no_verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    session = make_session(verify=not args.no_verify)

    mode = "w" if args.no_resume else "a"
    collected = 0
    skipped = 0
    failed = 0
    with open(jsonl_path, mode, encoding="utf-8") as jf:
        for i, (url, idntfc) in enumerate(targets, start=1):
            if idntfc and idntfc in done:
                skipped += 1
                continue
            try:
                html = fetch_detail(session, url, delay=args.delay)
                record = parse_detail(html, url)
                if idntfc and not record.get("물품식별번호"):
                    record["물품식별번호"] = idntfc
                jf.write(json.dumps(record, ensure_ascii=False) + "\n")
                jf.flush()
                collected += 1
                if idntfc:
                    done.add(idntfc)
            except Exception as err:  # noqa: BLE001 - 개별 실패는 로그 후 계속
                failed += 1
                print(f"  [{i}/{len(targets)}] 실패 {url}: {err}", file=sys.stderr)
                continue

            if collected % 20 == 0 or i == len(targets):
                print(f"  진행 {i}/{len(targets)} (수집 {collected}, 건너뜀 {skipped}, 실패 {failed})")
            time.sleep(args.delay)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = out_dir / f"detail_wide_{ts}.csv"
    n_rows, n_attrs = build_wide_csv(jsonl_path, out_csv)
    print(f"\n완료: 이번 수집 {collected}건 / 건너뜀 {skipped} / 실패 {failed}")
    print(f"Wide CSV: {out_csv}  (행 {n_rows}, 개별속성 컬럼 {n_attrs}개)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
