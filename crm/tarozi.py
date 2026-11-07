"""Reading a weighing scale's output — the server-side twin of static/js/tarozi.js.

The network receiver (`manage.py tarozi_listen`) gets the same bytes the Tarozi
sinovi page reads off a COM port, so it cuts and reads them by the same rules:
frames end at a line break (or sit between STX and ETX), a frame is decoded as
"ST,GS,+0012450kg" text, Yaohua A9, reversed digits after "=", or failing those the
first number in it; and a weight is stable when the scale says ST, or — when it says
nothing, like the `wn001.171kg` scales — when the same value arrived N times in a
row. Keep the two in step: a reading must not be stable on the page and not here.
"""
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

STX, ETX, CR, LF = 0x02, 0x03, 0x0D, 0x0A

#: A frame longer than this is not a weight line; the buffer is dropped rather
#: than grown, so a misbehaving sender cannot make the receiver hold its stream.
MAX_FRAME = 128

TEXT_RE = re.compile(
    r"\b(ST|US|OL)\b\s*,?\s*(GS|NT|TR|GW|NW|G|N)?\s*,?\s*([+-])?\s*(\d+(?:[.,]\d+)?)\s*(kg|g|t|lb)?",
    re.IGNORECASE)
NUMBER_RE = re.compile(r"([+-])?\s*(\d+(?:[.,]\d+)?)\s*(kg|g|t|lb)?", re.IGNORECASE)
FLAG_RE = re.compile(r"\b(ST|US)\b", re.IGNORECASE)


@dataclass
class Reading:
    kg: Decimal
    flag: str | None        # "ST" / "US" when the scale says so, else None
    raw: str                # the frame as text, control bytes dropped


def split_frames(buf: bytearray) -> list[bytes]:
    """Cut every complete frame off the front of `buf`, in place."""
    frames = []
    if STX in buf:
        while True:
            start = buf.find(bytes([STX]))
            if start == -1:
                buf.clear()
                break
            end = buf.find(bytes([ETX]), start + 1)
            if end == -1:
                del buf[:start]
                break
            frames.append(bytes(buf[start:end + 1]))
            del buf[:end + 1]
    else:
        while True:
            ends = [i for i in (buf.find(bytes([CR])), buf.find(bytes([LF]))) if i != -1]
            if not ends:
                break
            i = min(ends)
            if i:
                frames.append(bytes(buf[:i]))
            del buf[:i + 1]
    if len(buf) > MAX_FRAME:
        buf.clear()
    return frames


def _printable(frame: bytes) -> str:
    return "".join(chr(b) for b in frame if 0x20 <= b < 0x7F)


def _number(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def _to_kg(value: Decimal, unit: str | None) -> Decimal:
    unit = (unit or "kg").lower()
    if unit == "g":
        return value / 1000
    if unit == "t":
        return value * 1000
    return value


def _a9(frame: bytes, raw: str) -> Reading | None:
    """Yaohua XK3190-A9: STX, sign, 6 digits, decimal position, 2 XOR chars, ETX."""
    body = frame[1:9].decode("ascii", "replace")
    if not re.fullmatch(r"[+-]\d{6}[0-4]", body):
        return None
    x = 0
    for b in frame[1:9]:
        x ^= b
    got = frame[9:11].decode("ascii", "replace")
    if got not in (f"{x:02X}", chr(0x30 + (x >> 4)) + chr(0x30 + (x & 0xF))):
        return None
    value = Decimal(body[1:7]) / (Decimal(10) ** int(body[7]))
    return Reading(-value if body[0] == "-" else value, None, raw)


def decode(frame: bytes) -> Reading | None:
    """One frame → a reading, or None when it holds no usable weight (overload,
    noise, a heartbeat the converter slipped in)."""
    raw = _printable(frame)
    if len(frame) == 12 and frame[0] == STX and frame[-1] == ETX:
        return _a9(frame, raw)
    m = TEXT_RE.search(raw)
    if m:
        flag = m.group(1).upper()
        if flag == "OL":
            return None
        value = _number(m.group(4))
        if value is None:
            return None
        if m.group(3) == "-":
            value = -value
        return Reading(_to_kg(value, m.group(5)), flag, raw)
    if raw.startswith("="):
        m = NUMBER_RE.search(raw[1:][::-1])
        value = m and _number(m.group(2))
        if value is None:
            return None
        return Reading(-value if m.group(1) == "-" else value, None, raw)
    m = NUMBER_RE.search(raw)
    value = m and _number(m.group(2))
    if value is None:
        return None
    if m.group(1) == "-":
        value = -value
    flag = FLAG_RE.search(raw)
    return Reading(_to_kg(value, m.group(3)), flag.group(1).upper() if flag else None, raw)


class Stability:
    """Stable when the scale says ST, or — saying nothing — when the same value
    has arrived `need` times in a row."""

    def __init__(self, need: int = 5):
        self.need = need
        self.last: Decimal | None = None
        self.repeat = 0
        self.flag: str | None = None

    def push(self, kg: Decimal, flag: str | None) -> bool:
        if self.last is not None and kg == self.last:
            self.repeat += 1
        else:
            self.last, self.repeat = kg, 1
        self.flag = flag
        return self.stable

    @property
    def stable(self) -> bool:
        if self.last is None:
            return False
        if self.flag == "ST":
            return True
        if self.flag == "US":
            return False
        return self.repeat >= self.need
