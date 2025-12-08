"""toast_client.py

Toast API와 통신하기 위한 클라이언트 모듈.
"""
from __future__ import annotations

import logging
import time
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Generator, Iterable, List, Optional, Union

import requests
from requests import HTTPError


class ToastClient:
    """Toast Standard API 호출을 담당하는 헬퍼 클래스.

    매우 자세한 한글 주석을 통해 인증 관리, 공통 오류 처리, 페이지네이션 구현 방법을 설명한다.
    """

    # 429(Too Many Requests) 또는 5xx 서버 오류에 대응하기 위해 사용할 기본 재시도 횟수.
    _MAX_RETRIES: int = 5
    # 지수 백오프를 위한 기본 대기 시간(초). 재시도 시 2배씩 증가한다.
    _BACKOFF_FACTOR: float = 1.5

    def __init__(
        self,
        base_url: str,
        auth_url: str,
        client_id: str,
        client_secret: str,
        *,
        scopes: Optional[Iterable[str]] = None,
        session: Optional[requests.Session] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """클라이언트를 초기화한다.

        Args:
            base_url: Toast API의 기본 URL. 예) https://ws-api.toasttab.com
            auth_url: OAuth2 인증 요청을 보낼 URL.
            client_id: TOAST_MACHINE_CLIENT에서 발급받은 클라이언트 ID.
            client_secret: 클라이언트 비밀 키.
            session: requests.Session 객체. 테스트 시 주입 가능.
            logger: 외부에서 주입 가능한 Logger. 없으면 기본 로거 사용.
        """

        # Windows 환경에서도 문제없이 동작하도록 requests.Session을 재사용한다.
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.auth_url = auth_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.logger = logger or logging.getLogger(__name__)

        # 토큰과 만료 시각을 캐시하기 위한 내부 상태 변수.
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

        # OAuth 토큰 발급 시 포함할 scope 문자열을 미리 계산한다.
        normalized_scopes: List[str] = []
        for scope in scopes or []:
            token = (scope or "").strip()
            if token:
                normalized_scopes.append(token)
        self._scope_string = " ".join(normalized_scopes) if normalized_scopes else None

        # Orders API는 Toast 문서 기준으로 /orders/v2/ordersBulk 경로를 사용하므로
        # 별도의 버전 후보를 보관하지 않는다.
        self._orders_endpoint = self._build_detailed_orders_endpoint()

    # ------------------------------------------------------------------
    # 인증 관련 메서드
    # ------------------------------------------------------------------
    def authenticate(self) -> None:
        """OAuth2 토큰을 발급받아 내부 상태에 저장한다.

        Toast Standard API는 OAuth2 Client Credentials 방식을 사용한다. 요청 본문에는
        clientId/clientSecret을 JSON 형태로 전달해야 하며, 응답으로 accessToken과 만료 시간(초)을
        반환한다. Windows의 네트워크 환경에서도 문제가 없도록 timeout 값을 명시한다.
        """

        payload = {
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            # Toast 문서( Authentication and restaurant access )에 따르면 머신 계정은
            # userAccessType 값을 반드시 포함해야 한다.
            # 참고: https://doc.toasttab.com/doc/devguide/authentication.html
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        if self._scope_string:
            # 표준 OAuth2 스펙에 맞춰 scope 파라미터에 공백으로 구분된 권한을 전달한다.
            payload["scope"] = self._scope_string

        self.logger.debug("Toast API 인증 요청 시작")
        response = self.session.post(self.auth_url, json=payload, timeout=30)
        if response.status_code != 200:
            self.logger.error(
                "Toast API 인증 실패 - status: %s, body: %s",
                response.status_code,
                response.text,
            )
            response.raise_for_status()

        data = response.json()

        # Toast 인증 API는 배포 환경에 따라 토큰을 중첩 구조로 반환하기도 한다.
        #   { "token": { "accessToken": "...", "expiresInSec": 3600, ... } }
        # 과거 코드에서는 token(dict) 자체를 Bearer 값으로 써버려 401이 발생할 수 있었으므로
        # 중첩된 accessToken을 안전하게 추출한다.
        token_block = data.get("token") if isinstance(data.get("token"), dict) else {}

        token_candidates = [
            data.get("accessToken"),
            token_block.get("accessToken"),
            data.get("access_token"),
        ]
        access_token = next((value for value in token_candidates if isinstance(value, str) and value.strip()), None)

        # 만료 시간 키 역시 토큰 블록 안팎에 혼재할 수 있다. 숫자가 아닌 경우를 방지하기 위해
        # 우선 숫자형만 수집한 뒤 파싱한다.
        expires_in_candidates = [
            data.get("expiresIn"),
            data.get("expires_in"),
            data.get("expiresInSec"),
            token_block.get("expiresIn"),
            token_block.get("expires_in"),
            token_block.get("expiresInSec"),
        ]
        expires_in_raw = next(
            (value for value in expires_in_candidates if value is not None),
            3600,
        )

        if not access_token:
            # 응답 전체를 로깅하여 토큰 발급 실패 원인을 쉽게 파악할 수 있게 한다.
            self.logger.error("Toast API 인증 응답: %s", data)
            raise RuntimeError("Toast API 인증 응답에 accessToken이 없습니다.")

        try:
            expires_in = int(expires_in_raw)
        except (TypeError, ValueError):
            # 숫자로 해석할 수 없는 경우 기본 만료 시간을 1시간으로 가정한다.
            self.logger.warning(
                "만료 시간 파싱 실패(expiresIn=%s). 기본값 3600초를 사용합니다.", expires_in_raw
            )
            expires_in = 3600

        # 만료 시간 계산 (조금 여유를 두기 위해 30초 먼저 만료되는 것으로 설정).
        self._access_token = access_token
        expires_delta = max(expires_in - 30, 30)
        self._token_expiry = datetime.utcnow() + timedelta(seconds=expires_delta)
        self.logger.info("Toast API 토큰 발급 성공. 만료 예정 시각(UTC): %s", self._token_expiry)

    def _is_token_valid(self) -> bool:
        """현재 캐시된 토큰이 유효한지 확인한다."""

        if not self._access_token or not self._token_expiry:
            return False
        return datetime.utcnow() < self._token_expiry

    # ------------------------------------------------------------------
    # 공통 요청 래퍼
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        max_retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        """공통 요청 처리 로직.

        1. 토큰 유효성 검사 후 필요 시 자동 갱신
        2. 401 응답 처리: 최초 한 번은 토큰 갱신 후 재시도, 이후에는 권한 없음으로 판단
        3. 429/5xx 응답 처리: 지수 백오프 기반 재시도
        4. 모든 요청/응답을 상세 로그로 남겨 트러블슈팅을 돕는다.
        """

        if max_retries is None:
            max_retries = self._MAX_RETRIES
        max_attempts = max_retries + 1  # 최초 시도 1회 + 재시도 횟수

        # 토큰이 유효하지 않다면 즉시 갱신
        if not self._is_token_valid():
            self.authenticate()

        request_headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "BBQ-Toast-ETL/1.0",
        }
        if headers:
            request_headers.update(headers)

        attempt = 0
        backoff = self._BACKOFF_FACTOR
        last_exception: Optional[Exception] = None

        while attempt < max_attempts:
            attempt += 1
            try:
                self.logger.debug(
                    "Toast API 호출 - %s %s - params=%s",
                    method,
                    url,
                    params,
                )
                response = self.session.request(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    json=json,
                    timeout=60,
                )

                if response.status_code == 401:
                    # Toast 문서 기준 code=10013(Full authentication is required ...)은
                    # 매장 권한이 없다는 의미이므로, 토큰을 재발급해도 해결되지 않는다.
                    # 반면 토큰 만료 등으로 401이 내려오는 경우도 있으므로, 재발급 여부를
                    # 응답 바디를 기반으로 판단한다.
                    if attempt == 1 and self._should_refresh_token_on_401(response):
                        self.logger.warning("401 Unauthorized - 토큰 재발급 후 재시도")
                        self.authenticate()
                        request_headers["Authorization"] = f"Bearer {self._access_token}"
                        continue

                    self.logger.error("401 Unauthorized - 권한 없음으로 처리")
                    response.raise_for_status()

                if response.status_code in {429} or 500 <= response.status_code < 600:
                    if attempt >= max_attempts:
                        self.logger.error(
                            "Toast API 호출 실패(재시도 한도 초과) - status=%s body=%s",
                            response.status_code,
                            response.text,
                        )
                        response.raise_for_status()

                    wait_seconds = backoff ** attempt
                    self.logger.warning(
                        "Toast API 일시적 오류 감지(status=%s) - %s초 후 재시도 (%s/%s)",
                        response.status_code,
                        wait_seconds,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(wait_seconds)
                    continue

                response.raise_for_status()
                self.logger.debug(
                    "Toast API 호출 성공 - status=%s", response.status_code
                )
                return response.json()

            except HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if status_code in {401, 403}:
                    body_text = (
                        exc.response.text if exc.response is not None else "<no body>"
                    )
                    self.logger.error(
                        "Toast API 요청이 권한 오류(status=%s)로 실패했습니다. 응답 본문: %s",
                        status_code,
                        body_text,
                    )
                    raise

                last_exception = exc
                if attempt >= max_attempts:
                    self.logger.exception("Toast API 요청 중 HTTP 오류 발생 - 재시도 불가")
                    raise

                wait_seconds = backoff ** attempt
                self.logger.warning(
                    "Toast API HTTP 오류(status=%s) - %s초 후 재시도 (%s/%s)",
                    status_code,
                    wait_seconds,
                    attempt,
                    max_attempts,
                )
                time.sleep(wait_seconds)

            except requests.RequestException as exc:  # 네트워크 오류 등을 포착
                last_exception = exc
                if attempt >= max_attempts:
                    self.logger.exception("Toast API 요청 중 네트워크 오류 발생 - 재시도 불가")
                    raise

                wait_seconds = backoff ** attempt
                self.logger.warning(
                    "Toast API 요청 예외 발생(%s) - %s초 후 재시도 (%s/%s)",
                    exc,
                    wait_seconds,
                    attempt,
                    max_attempts,
                )
                time.sleep(wait_seconds)

        # 루프를 빠져나온 경우 마지막 예외를 다시 발생시켜 호출자에게 전달한다.
        if last_exception:
            raise last_exception
        raise RuntimeError("Toast API 요청이 실패했지만 구체적인 예외를 확인할 수 없습니다.")

    # ------------------------------------------------------------------
    # Orders API 전용 래퍼
    # ------------------------------------------------------------------
    def get_orders_for_business_date(
        self,
        restaurant_guid: str,
        business_date: Union[str, datetime, date],
        *,
        restaurant_external_id: Optional[str] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """특정 매장의 특정 영업일 주문을 페이지네이션하며 생성한다.

        Args:
            restaurant_guid: Toast에서 발급한 매장 GUID.
            business_date: date/datetime 또는 문자열(YYYY-MM-DD/yyyymmdd) 중 하나로 전달 가능하며,
                내부에서 yyyymmdd 형식으로 변환된다.
            restaurant_external_id: Toast Back Office에서 정의한 External ID.
                별도의 External ID를 모를 경우 GUID를 그대로 사용해도 되지만,
                토큰 권한과 매장 매핑이 외부 ID 기준으로 설정된 경우에는
                해당 값을 명시해야 401 오류를 피할 수 있다.

        Yields:
            주문 상세 JSON 객체.
        """

        yield from self._paginate_orders(
            self._orders_endpoint,
            restaurant_guid,
            business_date,
            restaurant_external_id,
        )

    # ------------------------------------------------------------------
    # 헬퍼 메서드
    # ------------------------------------------------------------------
    def _build_detailed_orders_endpoint(self) -> str:
        """Detailed Orders API 엔드포인트 문자열을 구성한다."""

        # Toast 문서 "Get multiple orders"에 명시된 경로는 /orders/v2/ordersBulk 이다.
        return f"{self.base_url}/orders/v2/ordersBulk"

    def _paginate_orders(
        self,
        endpoint: str,
        restaurant_guid: str,
        business_date: Union[str, datetime, date],
        restaurant_external_id: Optional[str],
    ) -> Generator[Dict[str, Any], None, None]:
        """페이지네이션 처리를 공통화한 제너레이터."""

        # Toast 권한은 외부 ID 기준으로 연결되는 경우가 많으므로, 별도로 입력된
        # external_id가 있다면 우선 사용한다. 없으면 GUID를 그대로 사용한다.
        external_id = restaurant_external_id or restaurant_guid
        headers = self._build_orders_headers(external_id)

        # Orders Bulk API는 businessDate를 yyyymmdd 형식으로 요구하므로 여기서 변환한다.
        api_business_date = self._coerce_business_date(business_date)

        page_number = 1
        next_page_token: Optional[str] = None
        use_page_token = False

        while True:
            params: Dict[str, Any] = {
                "businessDate": api_business_date,
                "pageSize": 100,
            }
            if use_page_token and next_page_token:
                params["pageToken"] = next_page_token
            else:
                params["page"] = page_number

            response_json = self._request(
                "GET",
                endpoint,
                headers=headers,
                params=params,
            )

            # -----------------------
            # 응답 구조 방어적 파싱
            # -----------------------
            # ordersBulk는 일반적으로 {"orders": [...]} 형태를 반환하지만, 일부
            # 테넌트/버전에서는 루트가 곧바로 리스트인 응답이 관찰될 수 있다. 또한
            # 예상치 못한 타입이 내려올 때 AttributeError가 발생하지 않도록 최대한
            # 관대하게 처리한다.
            if isinstance(response_json, list):
                orders_iterable: Iterable[object] = response_json
                next_page_token = None
                has_more = False
                next_page_value = None
            elif isinstance(response_json, dict):
                orders_iterable = response_json.get("orders", [])
                next_page_token = response_json.get("nextPageToken")
                has_more = response_json.get("hasMore")
                next_page_value = response_json.get("nextPage") or response_json.get("nextPageNumber")
            else:
                self.logger.error(
                    "예상치 못한 Orders 응답 타입(%s) - 페이징을 중단합니다: %r",
                    type(response_json).__name__,
                    response_json,
                )
                break

            for order in orders_iterable:
                if isinstance(order, dict):
                    yield order
                else:
                    self.logger.warning(
                        "주문 페이로드가 dict가 아니므로 스킵합니다: %r", order
                    )

            # API 사양에 따라 hasMore/nextPage/nextPageToken 등 다양한 형태가 존재하므로
            # 응답에 포함된 힌트를 우선적으로 사용한다. (루트가 리스트인 경우에는
            # 추가 페이지 정보를 알 수 없으므로 즉시 종료한다.)
            if isinstance(response_json, list):
                break

            if next_page_token:
                use_page_token = True
                continue

            if has_more or next_page_value:
                try:
                    page_number = int(next_page_value)
                except (TypeError, ValueError):
                    page_number += 1
                continue

            break

        # INFO 레벨로 매장별 완료 로그를 남기면 일자/지역별 백필 시
        # 로그가 과도하게 길어져 가독성을 해치므로 디버그 레벨로 내린다.
        self.logger.debug(
            "매장 %s의 %s 주문 데이터를 모두 수신했습니다. (endpoint=%s)",
            restaurant_guid,
            api_business_date,
            endpoint,
        )

    @staticmethod
    def _coerce_business_date(value: Union[str, datetime, date]) -> str:
        """Toast Orders API가 요구하는 yyyymmdd 형태로 businessDate를 강제한다."""

        if isinstance(value, datetime):
            return value.date().strftime("%Y%m%d")

        if isinstance(value, date):
            return value.strftime("%Y%m%d")

        if isinstance(value, str):
            cleaned = value.strip()
            if re.fullmatch(r"\d{8}", cleaned):
                return cleaned
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
                return cleaned.replace("-", "")

        raise ValueError(
            "businessDate는 YYYY-MM-DD 또는 yyyymmdd 문자열(또는 date/datetime 객체)만 허용됩니다."
        )

    @staticmethod
    def _build_orders_headers(external_id: str) -> Dict[str, str]:
        """Orders Bulk 엔드포인트가 요구하는 매장 식별 헤더를 구성한다."""

        # 공식 문서에서는 `Toast-Restaurant-External-ID`를 사용하지만 일부 예제에서는
        # `Toast-Location-External-Id`를 병행하기도 하므로 두 헤더를 모두 채운다.
        return {
            "Toast-Restaurant-External-ID": external_id,
            "Toast-Location-External-Id": external_id,
        }

    @staticmethod
    def _should_refresh_token_on_401(response: requests.Response) -> bool:
        """401 응답 시 토큰 재발급이 의미가 있는지 여부를 판단한다."""

        try:
            payload = response.json()
        except ValueError:
            # JSON이 아니라면 토큰 만료일 가능성이 있으므로 한 번은 재시도한다.
            return True

        code = payload.get("code")
        message_key = (payload.get("messageKey") or "").lower()
        message = (payload.get("message") or "").lower()

        # code=10013 혹은 "Full authentication is required..." 메시지는 권한 미부여 상황을 의미한다.
        if code == 10013 or str(code) == "10013":
            return False
        if "full authentication is required" in message_key or "full authentication is required" in message:
            return False

        return True


__all__ = ["ToastClient"]
