"""Mock of the Rivermont County property records SOAP service.

Stands in for a county system PermitFlow does not control, so a fresh clone runs end to
end with no external dependency (NFR-06). It speaks real SOAP 1.1 over HTTP and serves the
same WSDL the county publishes, which means the client in `property_records.py` is a real
zeep client doing a real round trip rather than a stub with a mocked return value.

Envelopes are built by hand rather than with a SOAP server framework because spyne does
not import on Python 3.12. Hand-building them is about forty lines and removes a
dependency that was only ever going to be used here.

Run it with:
    uvicorn permitflow.integrations.soap_mock.server:app --port 8081

The failure controls at the bottom exist to test NFR-02 and NFR-03. A resilience policy
you cannot demonstrate failing is a resilience policy nobody should believe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request, Response
from lxml import etree

NS = "http://records.rivermontcounty.gov/propertyrecords"
SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
WSDL_PATH = Path(__file__).with_name("property_records.wsdl")


@dataclass
class Parcel:
    apn: str
    situs_address: str
    zoning_code: str
    owner_name: str
    acreage: float
    stop_work_order: bool = False
    last_assessed_date: str = "2026-01-15"


PARCELS: dict[str, Parcel] = {
    p.apn: p
    for p in [
        Parcel("14-220-118", "1408 Aldergate Ln", "R-90", "Jordan Rivas", 0.31),
        Parcel("14-220-119", "1412 Aldergate Ln", "R-90", "Priya Venkataraman", 0.29),
        Parcel("09-114-007", "88 Fenwick Row", "R-60", "Marcus Deel", 0.16),
        Parcel("09-114-052", "104 Fenwick Row", "R-60", "Angela Sutton-Reyes", 0.17),
        Parcel("22-408-001", "3200 Copperline Blvd", "C-2", "Copperline Holdings LLC", 4.80),
        Parcel("22-408-014", "3260 Copperline Blvd", "C-2", "Vaughn Retail Partners LP", 2.35),
        Parcel("31-077-220", "17 Millrace Ct", "R-200", "Dana Whitfield", 1.05),
        Parcel("18-330-045", "640 Tanner St", "MXD", "Tanner Street Development Co", 0.92),
        Parcel("05-201-330", "9 Quarry Lane", "R-60", "Estate of H. Bramble", 0.22, stop_work_order=True),
        Parcel("27-190-088", "455 Ironwood Way", "I-1", "Ironwood Logistics Inc", 6.40),
    ]
}


@dataclass
class FailureMode:
    """Injected faults, controlled over the admin routes below.

    `fail_next` is a countdown rather than a flag so a test can prove the client retries
    twice and succeeds on the third attempt, which is the behaviour NFR-03 specifies.
    """

    outage: bool = False
    fail_next: int = 0
    delay_seconds: float = 0.0
    request_log: list[str] = field(default_factory=list)


failures = FailureMode()

app = FastAPI(title="Rivermont County Property Records (mock)")


def _fault(code: str, name: str, message: str, extra: dict[str, str] | None = None) -> Response:
    detail_children = "".join(
        f"<ns:{k}>{v}</ns:{k}>" for k, v in ({"Message": message} | (extra or {})).items()
    )
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{SOAP_ENV}" xmlns:ns="{NS}">
  <soap:Body>
    <soap:Fault>
      <faultcode>{code}</faultcode>
      <faultstring>{message}</faultstring>
      <detail>
        <ns:{name}>{detail_children}</ns:{name}>
      </detail>
    </soap:Fault>
  </soap:Body>
</soap:Envelope>"""
    return Response(content=body, media_type="text/xml", status_code=500)


#: The address published in the checked-in WSDL. Rewritten on the way out, see below.
DECLARED_ADDRESS = "http://localhost:8081/property-records"


@app.get("/property-records")
async def wsdl(request: Request) -> Response:
    """Serve the WSDL. zeep fetches this before the first call.

    The `soap:address` in the file is rewritten to whatever host actually served this
    request. A SOAP client reads the WSDL from wherever you point it but then posts to the
    address inside the document, so a hardcoded one sends every call to port 8081 no
    matter which instance you asked. That is a genuinely confusing failure: the client
    appears to work while ignoring the server under test.
    """
    if "wsdl" not in request.query_params:
        return Response(content="expected ?wsdl", status_code=400)

    served_from = str(request.url.replace(query="", fragment=""))
    document = WSDL_PATH.read_text().replace(DECLARED_ADDRESS, served_from)
    return Response(content=document, media_type="text/xml")


@app.post("/property-records")
async def get_parcel(request: Request) -> Response:
    raw = await request.body()

    if failures.delay_seconds:
        await asyncio.sleep(failures.delay_seconds)

    if failures.outage:
        return _fault("soap:Server", "ServiceUnavailableFault", "records system is offline for maintenance")

    if failures.fail_next > 0:
        failures.fail_next -= 1
        return _fault("soap:Server", "ServiceUnavailableFault", "temporary upstream failure")

    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return _fault("soap:Client", "ServiceUnavailableFault", "malformed request envelope")

    apn_nodes = root.findall(f".//{{{NS}}}APN")
    if not apn_nodes or not (apn_nodes[0].text or "").strip():
        return _fault("soap:Client", "ParcelNotFoundFault", "APN is required", {"APN": ""})

    apn = apn_nodes[0].text.strip()
    failures.request_log.append(apn)

    parcel = PARCELS.get(apn)
    if parcel is None:
        return _fault("soap:Server", "ParcelNotFoundFault", f"no parcel on record for {apn}", {"APN": apn})

    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="{SOAP_ENV}" xmlns:ns="{NS}">
  <soap:Body>
    <ns:GetParcelByAPNResponse>
      <ns:APN>{parcel.apn}</ns:APN>
      <ns:SitusAddress>{parcel.situs_address}</ns:SitusAddress>
      <ns:ZoningCode>{parcel.zoning_code}</ns:ZoningCode>
      <ns:OwnerName>{parcel.owner_name}</ns:OwnerName>
      <ns:Acreage>{parcel.acreage}</ns:Acreage>
      <ns:StopWorkOrder>{str(parcel.stop_work_order).lower()}</ns:StopWorkOrder>
      <ns:LastAssessedDate>{parcel.last_assessed_date}</ns:LastAssessedDate>
    </ns:GetParcelByAPNResponse>
  </soap:Body>
</soap:Envelope>"""
    return Response(content=body, media_type="text/xml")


# ---------------------------------------------------------------------------
# Failure injection. Not part of the county's contract, obviously.
# ---------------------------------------------------------------------------

@app.post("/admin/failure")
async def set_failure(
    outage: bool = False, fail_next: int = 0, delay_seconds: float = 0.0
) -> dict[str, object]:
    failures.outage = outage
    failures.fail_next = fail_next
    failures.delay_seconds = delay_seconds
    return {"outage": outage, "fail_next": fail_next, "delay_seconds": delay_seconds}


@app.post("/admin/reset")
async def reset() -> dict[str, str]:
    failures.outage = False
    failures.fail_next = 0
    failures.delay_seconds = 0.0
    failures.request_log.clear()
    return {"status": "reset"}


@app.get("/admin/requests")
async def requests_seen() -> dict[str, object]:
    """Attempt log, so a test can count retries rather than infer them."""
    return {"count": len(failures.request_log), "apns": failures.request_log}
