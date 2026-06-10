"""조달청 목록정보시스템(goods.g2b.go.kr) 품목검색 크롤러.

검색조건 목록(input_terms.csv)을 배치로 입력받아 각 조건의 전체 페이지를
순회 수집하고 CSV로 저장한다.

사용 예:
    python crawler.py
    python crawler.py --input input_terms.csv --out-dir output --page-size 100 --delay 0.7
"""

import argparse
import csv
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://goods.g2b.go.kr:8053"
SEARCH_PATH = "/search/productSearch.do"
SEARCH_URL = BASE_URL + SEARCH_PATH

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 검색조건으로 허용되는 폼 필드 (input_terms.csv 의 field 컬럼 값)
ALLOWED_SEARCH_FIELDS = {
    "searchGoodsClsfcNo",       # 세부품명번호
    "searchGoodsIdntfcNo",      # 물품식별번호
    "searchUpperGoodsClsfcNm",  # 품명
    "searchGoodsClsfcNm",       # 세부품명
    "searchGoodsNm",            # 품목명
    "searchCrtfcDivCd",         # 연계인증
}

CSV_FIELDNAMES = [
    "검색조건",
    "세부품명번호",
    "물품식별번호",
    "품명",
    "품목명",
    "품목구분",
    "이미지URL",
    "상세URL",
]


def make_session(verify=True):
    """세션 생성 후 최초 GET 으로 쿠키(JSESSIONID, hsessid)를 확보한다."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": SEARCH_URL,
        }
    )
    session.verify = verify
    # 세션 쿠키 확보
    resp = session.get(SEARCH_URL, timeout=30)
    resp.raise_for_status()
    return session


def search_page(session, criteria, page, page_size, max_retries=3, delay=0.7):
    """검색 폼을 POST 하고 응답 HTML(str)을 반환한다.

    criteria: {field: value} 형태의 검색조건 (예: {"searchGoodsClsfcNo": "5611210201"})
    """
    data = {
        "mode": "search",
        "pageNumber": str(page),
        "pageSize": str(page_size),
    }
    data.update(criteria)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.post(SEARCH_URL, data=data, timeout=60)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"서버 오류 {resp.status_code}")
            resp.raise_for_status()
            resp.encoding = "UTF-8"
            return resp.text
        except (requests.RequestException, requests.HTTPError) as err:
            last_err = err
            backoff = delay * (2 ** (attempt - 1))
            print(
                f"    [재시도 {attempt}/{max_retries}] page={page} 요청 실패: {err} "
                f"-> {backoff:.1f}s 대기",
                file=sys.stderr,
            )
            time.sleep(backoff)
    raise RuntimeError(f"page={page} 요청을 {max_retries}회 시도했으나 실패: {last_err}")


def parse_total_count(html):
    """'총 139,565건이 검색되었습니다' 형태에서 총 건수(int)를 파싱한다.

    찾지 못하면 None 을 반환한다.
    """
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one("p.tit-searchNum")
    if el is None:
        return None
    text = el.get_text(strip=True)
    m = re.search(r"([\d,]+)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_image_url(td):
    """이미지 셀에서 원본 이미지 경로를 추출한다. 없으면 빈 문자열."""
    if td is None:
        return ""
    # 1) <a href="...fileFullPath=2026%2f06%2f10%2f...jpg">
    a = td.find("a", href=True)
    if a and "fileFullPath=" in a["href"]:
        qs = parse_qs(urlparse(a["href"]).query)
        full = qs.get("fileFullPath", [""])[0]
        if full:
            return urljoin(BASE_URL, "/product/productImageViewTagPopup.do?fileFullPath=" + full)
    # 2) <img src="..."> (예: noImage.jpg)
    img = td.find("img")
    if img and img.get("src"):
        return urljoin(BASE_URL, img["src"])
    return ""


def _extract_detail(td):
    """물품식별번호 셀에서 식별번호와 상세 URL을 추출한다."""
    if td is None:
        return "", ""
    a = td.find("a", href=True)
    if not a:
        # 링크 없이 텍스트만 있는 경우
        return td.get_text(strip=True), ""
    idntfc = a.get_text(strip=True)
    detail_url = urljoin(SEARCH_URL, a["href"])
    return idntfc, detail_url


def parse_rows(html, criteria_label):
    """결과 표(table.tableType_List)의 tbody tr 을 파싱해 dict 리스트로 반환한다."""
    soup = BeautifulSoup(html, "lxml")
    tbody = soup.select_one("#searchVO div.listWrap table tbody")
    if tbody is None:
        return []

    rows = []
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 6:
            # 데이터 없음 안내행 등은 건너뜀
            continue

        image_url = _extract_image_url(tds[0])
        goods_clsfc_no = tds[1].get_text(strip=True)
        idntfc_no, detail_url = _extract_detail(tds[2])
        clsfc_nm = tds[3].get_text(strip=True)
        goods_nm = tds[4].get_text(" ", strip=True)
        goods_div = tds[5].get_text(strip=True)

        rows.append(
            {
                "검색조건": criteria_label,
                "세부품명번호": goods_clsfc_no,
                "물품식별번호": idntfc_no,
                "품명": clsfc_nm,
                "품목명": goods_nm,
                "품목구분": goods_div,
                "이미지URL": image_url,
                "상세URL": detail_url,
            }
        )
    return rows


def _open_csv_writer(path):
    """UTF-8 BOM CSV 파일을 열고 헤더를 기록한 (file, writer) 를 반환한다."""
    f = open(path, "w", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
    writer.writeheader()
    return f, writer


def crawl_criteria(session, criteria, page_size, delay, max_pages,
                   combined_writer=None, out_dir=None, dedup=True):
    """단일 검색조건의 전체 페이지를 순회 수집한다.

    조건별 CSV(out_dir) 와 통합 CSV(combined_writer) 양쪽에 기록한다.
    수집한 행 수를 반환한다.
    """
    criteria_label = "&".join(f"{k}={v}" for k, v in criteria.items())
    print(f"\n[검색조건] {criteria_label}")

    # 조건별 파일 준비
    per_file = None
    per_writer = None
    if out_dir is not None:
        safe = re.sub(r"[^0-9A-Za-z._-]", "_", criteria_label) or "result"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        per_path = Path(out_dir) / f"{safe}_{ts}.csv"
        per_file, per_writer = _open_csv_writer(per_path)
        print(f"  조건별 출력: {per_path}")

    seen = set()
    total_written = 0
    prev_first_id = None

    # 1페이지 수집 + 총 건수 파악
    html = search_page(session, criteria, 1, page_size, delay=delay)
    total_count = parse_total_count(html)
    if total_count is None:
        total_pages = max_pages
        print("  총 건수 파싱 실패 -> 빈 페이지를 만날 때까지 진행")
    else:
        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0
        print(f"  총 {total_count:,}건 / 페이지당 {page_size}건 -> 약 {total_pages}페이지")

    if max_pages:
        total_pages = min(total_pages, max_pages) if total_pages else max_pages

    page = 1
    while True:
        if page > 1:
            html = search_page(session, criteria, page, page_size, delay=delay)

        rows = parse_rows(html, criteria_label)
        if not rows:
            print(f"  page={page}: 행 없음 -> 종료")
            break

        first_id = rows[0]["물품식별번호"]
        if prev_first_id is not None and first_id == prev_first_id:
            print(f"  page={page}: 직전 페이지와 동일 -> 종료")
            break
        prev_first_id = first_id

        page_written = 0
        for row in rows:
            key = row["물품식별번호"]
            if dedup and key and key in seen:
                continue
            if key:
                seen.add(key)
            if per_writer:
                per_writer.writerow(row)
            if combined_writer:
                combined_writer.writerow(row)
            page_written += 1
            total_written += 1

        if per_file:
            per_file.flush()
        print(f"  page={page}/{total_pages or '?'} 수집 {page_written}건 (누적 {total_written}건)")

        if total_pages and page >= total_pages:
            break
        page += 1
        time.sleep(delay)

    if per_file:
        per_file.close()
    print(f"  -> 조건 완료: 총 {total_written}건 수집")
    return total_written


def load_terms(input_path):
    """input_terms.csv 를 읽어 검색조건 dict 리스트를 반환한다."""
    terms = []
    with open(input_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "field" not in reader.fieldnames or "value" not in reader.fieldnames:
            raise ValueError("input CSV 는 'field,value' 헤더를 가져야 합니다.")
        for i, raw in enumerate(reader, start=2):
            field = (raw.get("field") or "").strip()
            value = (raw.get("value") or "").strip()
            if not field and not value:
                continue
            if field not in ALLOWED_SEARCH_FIELDS:
                print(
                    f"  [경고] {input_path}:{i} 알 수 없는 검색필드 '{field}' -> 건너뜀 "
                    f"(허용: {', '.join(sorted(ALLOWED_SEARCH_FIELDS))})",
                    file=sys.stderr,
                )
                continue
            terms.append({field: value})
    return terms


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="조달청 목록정보시스템 품목 크롤러")
    p.add_argument("--input", default="input_terms.csv", help="검색조건 CSV 경로 (field,value)")
    p.add_argument("--out-dir", default="output", help="결과 CSV 저장 폴더")
    p.add_argument("--page-size", type=int, default=100, choices=[10, 20, 30, 50, 100],
                   help="페이지당 건수 (기본 100)")
    p.add_argument("--delay", type=float, default=0.7, help="요청 간 지연(초)")
    p.add_argument("--max-pages", type=int, default=0,
                   help="조건별 최대 페이지 수 (0=제한 없음)")
    p.add_argument("--no-dedup", action="store_true", help="물품식별번호 중복 제거 비활성화")
    p.add_argument("--no-verify", action="store_true", help="SSL 인증서 검증 비활성화")
    p.add_argument("--no-combined", action="store_true", help="통합 CSV 생성 안 함")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    terms = load_terms(args.input)
    if not terms:
        print("수집할 검색조건이 없습니다. input CSV 를 확인하세요.", file=sys.stderr)
        return 1
    print(f"검색조건 {len(terms)}건 로드 완료")

    if args.no_verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    session = make_session(verify=not args.no_verify)

    combined_file = None
    combined_writer = None
    if not args.no_combined:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        combined_path = out_dir / f"combined_{ts}.csv"
        combined_file, combined_writer = _open_csv_writer(combined_path)
        print(f"통합 출력: {combined_path}")

    grand_total = 0
    try:
        for term in terms:
            grand_total += crawl_criteria(
                session,
                term,
                page_size=args.page_size,
                delay=args.delay,
                max_pages=args.max_pages,
                combined_writer=combined_writer,
                out_dir=out_dir,
                dedup=not args.no_dedup,
            )
            if combined_file:
                combined_file.flush()
    finally:
        if combined_file:
            combined_file.close()

    print(f"\n완료: 전체 {grand_total}건 수집")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
