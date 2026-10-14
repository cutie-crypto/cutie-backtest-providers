"""Regenerate the real-price slice from a checksummed Binance USD-M archive."""
import csv
import hashlib
import io
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

URL = "https://data.binance.vision/data/futures/um/monthly/klines/PUMPUSDT/4h/PUMPUSDT-4h-2026-07.zip"
SHA256 = "6c2e209262b76fe4f30bf28ef18d90a14d162b883cbe05d1c63f3da251d7119a"

if __name__ == "__main__":
    with urlopen(URL, timeout=60) as response:
        archive = response.read()
    with urlopen(URL + ".CHECKSUM", timeout=60) as response:
        official = response.read().decode().split()[0]
    assert hashlib.sha256(archive).hexdigest() == official == SHA256
    with ZipFile(io.BytesIO(archive)) as zipped:
        rows = csv.reader(io.StringIO(zipped.read(zipped.namelist()[0]).decode()))
        selected = [row for row in rows if row[0].isdigit()
                    and 1783699200000 <= int(row[0]) <= 1784260800000]
    assert len(selected) == 40
    Path(__file__).with_name("pump_unlock_4h_202607.csv").write_text(
        "".join(",".join(row) + "\n" for row in selected), encoding="utf-8")
