#!/usr/bin/env python3
"""Install the ISPF option P (COPILOT) dialog on an MVS 3.8j system via mvsMF.

Uploads the CLIST and panel to <HLQ>.CMDPROC and <HLQ>.ISP.PLIB and adds a
"P COPILOT" line to <HLQ>.ISP.PLIB(ISP@PRIM). The vendor ISPF libraries are
never modified: ISPLOGON concatenates <userid>.ISP.PLIB ahead of them.

Env: ZOSMF_USER, ZOSMF_PASSWORD (required); ZOSMF_BASE_URL
(default http://127.0.0.1:8090).
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MENU_ANCHOR = "T +TUTORIAL"
TRANS_ANCHOR = "T,'PGM(ISPTUTOR)"
MENU_LINE = "%   P +COPILOT     - Two-way messaging with GitHub Copilot"
TRANS_LINE = "                P,'CMD(%COPILOT)'"


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


class Zosmf:
    def __init__(self, base, user, password):
        self.base = base.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = {"Authorization": f"Basic {token}",
                     "X-CSRF-ZOSMF-HEADER": "x"}

    def call(self, method, dsn, data=None, headers=None):
        url = f"{self.base}/zosmf/restfiles/ds/{urllib.parse.quote(dsn, safe='()@./')}"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={**self.auth, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def read_binary(self, dsn):
        status, body = self.call("GET", dsn, headers={"X-IBM-Data-Type": "binary"})
        return body if status == 200 else None

    def write_text(self, dsn, text):
        status, body = self.call("PUT", dsn, text.encode("ascii"),
                                 {"Content-Type": "text/plain"})
        if status not in (200, 201, 204):
            die(f"writing {dsn} failed: HTTP {status} {body[:200]!r}")

    def write_binary(self, dsn, data):
        status, body = self.call("PUT", dsn, data, {
            "Content-Type": "application/octet-stream",
            "X-IBM-Data-Type": "binary"})
        if status not in (200, 201, 204):
            die(f"writing {dsn} failed: HTTP {status} {body[:200]!r}")

    def ensure_pds(self, dsn):
        status, _ = self.call("GET", f"{dsn}/member")
        if status == 200:
            return
        spec = {"dsorg": "PO", "recfm": "FB", "lrecl": 80, "blksize": 3120,
                "primary": 2, "secondary": 1, "dirblk": 20, "alcunit": "CYL"}
        status, body = self.call("POST", dsn, json.dumps(spec).encode(),
                                 {"Content-Type": "application/json"})
        if status not in (200, 201):
            die(f"creating {dsn} failed: HTTP {status} {body[:200]!r}")
        print(f"created {dsn}")


def records(blob):
    if len(blob) % 80:
        die("ISP@PRIM is not a multiple of 80 bytes; unexpected format")
    return [blob[i:i + 80] for i in range(0, len(blob), 80)]


def patch_prim(blob):
    """Insert the menu line and selection entry; return None if present."""
    recs = records(blob)
    text = [r.decode("cp037", "replace") for r in recs]
    if any("%COPILOT" in t for t in text):
        return None
    menu = [i for i, t in enumerate(text) if MENU_ANCHOR in t]
    trans = [i for i, t in enumerate(text) if TRANS_ANCHOR in t]
    if len(menu) != 1 or len(trans) != 1:
        die("could not find the tutorial menu line / TRANS entry in ISP@PRIM; "
            "add the two lines by hand (see README)")
    recs.insert(trans[0], TRANS_LINE.ljust(80).encode("cp037"))
    recs.insert(menu[0] + 1, MENU_LINE.ljust(80).encode("cp037"))
    return b"".join(recs)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hlq", help="target high-level qualifier (default: user)")
    ap.add_argument("--vendor-plib", default="ISP.V2R2M0.PLIB",
                    help="vendor panel library holding ISP@PRIM")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    user = os.environ.get("ZOSMF_USER") or die("ZOSMF_USER not set")
    password = os.environ.get("ZOSMF_PASSWORD") or die("ZOSMF_PASSWORD not set")
    base = os.environ.get("ZOSMF_BASE_URL", "http://127.0.0.1:8090")
    hlq = (args.hlq or user).upper()
    zos = Zosmf(base, user, password)

    status, _ = zos.call("GET", "SYS1.PARMLIB/member")  # reachability check
    if status in (401, 403):
        die("mvsMF rejected the credentials")

    clist = open(os.path.join(HERE, "src", "COPILOT.clist")).read()
    panel = open(os.path.join(HERE, "src", "COPILP1.panel")).read()
    cmdproc, plib = f"{hlq}.CMDPROC", f"{hlq}.ISP.PLIB"

    prim_src = zos.read_binary(f"{plib}(ISP@PRIM)")
    origin = plib
    if prim_src is None:
        prim_src = zos.read_binary(f"{args.vendor_plib}(ISP@PRIM)")
        origin = args.vendor_plib
    if prim_src is None:
        die(f"cannot read ISP@PRIM from {plib} or {args.vendor_plib}")
    patched = patch_prim(prim_src)
    print(f"ISP@PRIM source: {origin}"
          + (" (already has the COPILOT option)" if patched is None else ""))

    if args.dry_run:
        print("dry run: nothing written")
        return
    zos.ensure_pds(cmdproc)
    zos.ensure_pds(plib)
    zos.write_text(f"{cmdproc}(COPILOT)", clist)
    zos.write_text(f"{plib}(COPILP1)", panel)
    if patched is not None:
        zos.write_binary(f"{plib}(ISP@PRIM)", patched)
    print(f"installed: {cmdproc}(COPILOT), {plib}(COPILP1)"
          + (f", {plib}(ISP@PRIM)" if patched is not None else ""))
    print("log on to TSO and pick option P on the ISPF primary menu")


if __name__ == "__main__":
    main()
