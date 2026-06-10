# 조달청 목록정보시스템 품목 크롤러

[목록정보시스템 품목검색](https://goods.g2b.go.kr:8053//search/productSearch.do) 페이지에서
검색조건별 품목 데이터를 전체 페이지에 걸쳐 수집하여 CSV로 저장합니다.

## 동작 방식

- 검색 폼(`searchVO`)을 그대로 POST 요청하며, 결과 표(`table.tableType_List`)는 서버에서
  HTML로 렌더링되므로 브라우저(JS 실행) 없이 `requests` + `BeautifulSoup`만으로 수집합니다.
- 응답에 포함된 `총 N건이 검색되었습니다` 문구로 전체 건수를 파싱해 페이지 수를 계산하고,
  `pageNumber`를 증가시키며 마지막 페이지까지 순회합니다.
- 검색조건이 있을 때 페이지 이동이 동작하므로, 검색조건을 배치로 입력받습니다.

## 설치

```bash
pip install -r requirements.txt
```

## 검색조건 입력 (`input_terms.csv`)

`field,value` 헤더를 가진 CSV로 작성합니다. 한 줄이 하나의 검색조건입니다.

```csv
field,value
searchGoodsClsfcNo,5611210201
searchGoodsNm,의자
```

사용 가능한 `field`:

| field | 의미 |
| --- | --- |
| `searchGoodsClsfcNo` | 세부품명번호 |
| `searchGoodsIdntfcNo` | 물품식별번호 |
| `searchUpperGoodsClsfcNm` | 품명 |
| `searchGoodsClsfcNm` | 세부품명 |
| `searchGoodsNm` | 품목명 |
| `searchCrtfcDivCd` | 연계인증 |

## 실행

```bash
python crawler.py
```

옵션:

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--input` | `input_terms.csv` | 검색조건 CSV 경로 |
| `--out-dir` | `output` | 결과 CSV 저장 폴더 |
| `--page-size` | `100` | 페이지당 건수 (10/20/30/50/100) |
| `--delay` | `0.7` | 요청 간 지연(초) |
| `--max-pages` | `0` | 조건별 최대 페이지 수 (0=제한 없음) |
| `--no-dedup` | - | 물품식별번호 중복 제거 비활성화 |
| `--no-verify` | - | SSL 인증서 검증 비활성화 |
| `--no-combined` | - | 통합 CSV 생성 안 함 |

예시:

```bash
python crawler.py --page-size 100 --delay 1.0 --max-pages 5
```

## 출력

`output/` 폴더에 UTF-8 BOM(엑셀 호환) CSV가 생성됩니다.

- 조건별 파일: `<검색조건>_<타임스탬프>.csv`
- 통합 파일: `combined_<타임스탬프>.csv`

수집 컬럼: `검색조건, 세부품명번호, 물품식별번호, 품명, 품목명, 품목구분, 이미지URL, 상세URL`

## 상세 페이지 크롤러 (`detail_crawler.py`)

목록 크롤링 결과 CSV(`combined_*.csv` 등)의 `상세URL`을 읽어 각 품목 상세페이지
(`productSearchView.do`)를 수집합니다. 공통속성정보 + 가변 개별속성정보를 Wide 포맷
CSV로 저장합니다.

```bash
# output 폴더의 최신 combined_*.csv 를 자동으로 입력 사용
python detail_crawler.py

# 입력 파일/건수 지정 (테스트)
python detail_crawler.py --input output/combined_20260610_160536.csv --limit 20
```

옵션:

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `--input` | 최신 `combined_*.csv` | 상세URL이 담긴 입력 CSV |
| `--url-column` | `상세URL` | 상세URL 컬럼명 |
| `--out-dir` | `output` | 결과 저장 폴더 |
| `--delay` | `0.5` | 요청 간 지연(초) |
| `--limit` | `0` | 수집 최대 건수 (0=전체) |
| `--no-resume` | - | 이어받기 비활성화(처음부터 다시) |
| `--no-verify` | - | SSL 인증서 검증 비활성화 |

출력:

- `output/detail_records.jsonl` — 원본 레코드(진행분 즉시 저장, 이어받기용 체크포인트)
- `output/detail_wide_<타임스탬프>.csv` — 최종 Wide CSV(UTF-8 BOM)

Wide CSV 컬럼: 고정 컬럼(물품식별번호/세부품명번호/물품목록번호/물품분류번호/품명/
세부품명영문명/단위/내용연수/상품원산지국가명/품목구분/부품여부/품목등록일/모델명/
상품브랜드명/품목명/제조업체명/제품설명/분류경로/이미지URL/상세URL) + 가변 개별속성
컬럼(예: `크기(가로)`, `용도`, `색상` …, 값은 측정단위와 결합되어 `435 mm` 형태).

이어받기: 중간에 중단해도 `detail_records.jsonl`에 저장된 완료분은 재실행 시 자동으로
건너뜁니다. 처음부터 다시 받으려면 `--no-resume`을 사용합니다.

## 주의

- 정부 사이트이므로 `--delay`로 요청 간격을 두어 부하를 주지 않도록 합니다.
- 검색조건 없이 전체 목록은 페이지 이동이 고정되어 전수 수집이 되지 않습니다(검색조건 사용 권장).
