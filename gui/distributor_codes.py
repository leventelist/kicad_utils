"""Parse the 2D codes printed on distributor component bags.

Supported formats:

* ISO/IEC 15434 (format "06", ANSI MH10.8.2 data identifiers), used by
  DigiKey and Mouser Data Matrix codes:
  `[)>␞06␝P<distributor PN>␝1P<manufacturer PN>␝Q<quantity>␝…␞␄`
* LCSC QR codes: `{pbn:…,on:<order>,pc:<LCSC PN>,pm:<MPN>,qty:<n>,…}`
* TME QR codes: `QTY:<n> PN:<TME PN> MFR:<manufacturer> MPN:<MPN> PO:<order> …`

ISO 15434 separates fields with the control characters GS (0x1D) and RS
(0x1E). Many handheld scanners in keyboard mode drop them. The scan field
replaces them with the visible symbols ␝ and ␞, which are accepted here too,
as are the placeholders {GS} and <GS>. If the separators are lost
completely, the fields cannot be split reliably; parse() then returns a code
with `error` set.
"""

import re
from dataclasses import dataclass

GS, RS, EOT = "\x1d", "\x1e", "\x04"
# Visible stand-ins for the control characters (inserted by the scan field).
VISIBLE = {GS: "␝", RS: "␞", EOT: "␄"}


@dataclass
class DistributorCode:
    distributor: str             # "DigiKey", "Mouser", "LCSC", "TME" or "ISO 15434"
    part_number: str = ""        # the distributor's own part number (ordering code)
    mpn: str = ""                # manufacturer part number
    manufacturer: str = ""
    quantity: int | None = None
    order: str = ""              # order / purchase order number
    error: str = ""              # set if the code was recognised but not readable

    def describe(self):
        parts = [self.distributor]
        if self.part_number:
            parts.append(self.part_number)
        if self.mpn and self.mpn != self.part_number:
            parts.append(f"MPN {self.mpn}")
        if self.quantity is not None:
            parts.append(f"qty {self.quantity}")
        if self.order:
            parts.append(f"order {self.order}")
        return ", ".join(parts)


def _int(text):
    try:
        return int(str(text).strip())
    except ValueError:
        return None


def _normalise(code):
    for ch, visible in VISIBLE.items():
        code = code.replace(visible, ch)
    return (code.replace("{GS}", GS).replace("<GS>", GS)
                .replace("{RS}", RS).replace("<RS>", RS)
                .replace("{EOT}", EOT).replace("<EOT>", EOT))


def _iso15434(code):
    code = _normalise(code)
    if not code.startswith("[)>"):
        return None
    body = code[3:].replace(EOT, "").strip(RS)
    if not body.startswith("06"):
        return None
    fields = [f.strip(RS) for f in body[2:].split(GS)]
    fields = [f for f in fields if f]
    if len(fields) < 2:
        return DistributorCode(
            "ISO 15434", error="The field separators (GS) are missing. Configure the "
            "scanner to send them, for example as Ctrl+] or as {GS}.")
    data = {}
    for f in fields:
        m = re.match(r"(\d{0,3}[A-Z])(.*)$", f, re.S)
        if m:
            data.setdefault(m.group(1), m.group(2).strip())
    if "1V" in data or "14K" in data:
        # Mouser: K = PO, 14K = line, 1P = MPN, Q = qty, 11K = invoice, 1V = mfr
        return DistributorCode("Mouser", part_number=data.get("P", ""),
                               mpn=data.get("1P", ""), manufacturer=data.get("1V", ""),
                               quantity=_int(data.get("Q", "")),
                               order=data.get("11K") or data.get("K", ""))
    dist = "ISO 15434"
    if data.get("P", "").upper().endswith("-ND") or any(
            k in data for k in ("11Z", "12Z", "13Z", "20Z")):
        dist = "DigiKey"
    return DistributorCode(dist, part_number=data.get("P", ""),
                           mpn=data.get("1P", ""), quantity=_int(data.get("Q", "")),
                           order=data.get("1K") or data.get("K", ""))


def _lcsc(code):
    code = code.strip()
    if not (code.startswith("{") and "pc:" in code):
        return None
    data = {}
    for item in code.strip("{}").split(","):
        if ":" in item:
            k, v = item.split(":", 1)
            data[k.strip()] = v.strip()
    if not data.get("pc"):
        return None
    return DistributorCode("LCSC", part_number=data.get("pc", ""),
                           mpn=data.get("pm", ""), quantity=_int(data.get("qty", "")),
                           order=data.get("on", ""))


def _tme(code):
    if "QTY:" not in code or not ("PN:" in code or "MPN:" in code):
        return None
    data = dict(re.findall(r"\b(QTY|PN|MFR|MPN|PO|CoO):(\S*)", code))
    if not (data.get("PN") or data.get("MPN")):
        return None
    return DistributorCode("TME", part_number=data.get("PN", ""),
                           mpn=data.get("MPN", ""), manufacturer=data.get("MFR", ""),
                           quantity=_int(data.get("QTY", "")), order=data.get("PO", ""))


def parse(code):
    """Return a DistributorCode, or None if `code` is not a distributor code."""
    for parser in (_iso15434, _lcsc, _tme):
        result = parser(code)
        if result is not None:
            return result
    return None


# Supplier names (lower case substrings) per distributor, for matching rows
# of the `supplier` table.
SUPPLIER_NAMES = {
    "DigiKey": ("digikey", "digi-key"),
    "Mouser": ("mouser",),
    "LCSC": ("lcsc",),
    "TME": ("tme",),
}
