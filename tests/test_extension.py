"""Native-messaging host: framing + progress parsing (no browser needed)."""

def _load_host():
    import importlib.util
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "extension" / "host" / "siphon_host.py"
    spec = importlib.util.spec_from_file_location("siphon_host", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_host_framing_roundtrip(tmp_path, monkeypatch):
    import io
    import struct

    host = _load_host()
    msg = {"action": "hello"}
    data = __import__("json").dumps(msg).encode()
    monkeypatch.setattr(host.sys, "stdin", type("S", (), {"buffer": io.BytesIO(struct.pack("<I", len(data)) + data)})())
    assert host.read_message() == msg
    # send() must emit a length prefix + the JSON payload.
    out = io.BytesIO()
    monkeypatch.setattr(host.sys, "stdout", type("S", (), {"buffer": out})())
    host.send({"event": "hello", "version": "9.9"})
    buf = out.getvalue()
    (n,) = struct.unpack("<I", buf[:4])
    assert __import__("json").loads(buf[4:4 + n]) == {"event": "hello", "version": "9.9"}


def test_host_progress_parsing():
    host = _load_host()
    assert host.PCT.search("[download]  58.1% of 218KiB at 2.90MiB/s").group(1) == "58.1"
    assert host.SPEED.search("[download] 100% of 218KiB at 2.90MiB/s").group(1) == "2.90MiB/s"
    assert host.SAVED.search("✓ /home/u/Downloads/clip [ab12].mp4  28MB").group(1) == "/home/u/Downloads/clip [ab12].mp4"
    assert host.SAVED.search('[download] Destination: /tmp/My Video.webm').group(1) == "/tmp/My Video.webm"


def test_host_reads_saved_files_from_provenance(tmp_path):
    host = _load_host()
    prov = tmp_path / "siphon.jsonl"
    prov.write_text('{"file": "old.mp4"}\n')
    since = prov.stat().st_size
    # A run appends two records (e.g. a multi-image tweet); only the new ones count.
    with open(prov, "a") as f:
        f.write('{"file": "a [emoji \U0001f577️].mkv"}\n{"file": "b.jpg"}\n')
    got = host.read_saved(prov, since, tmp_path)
    assert got == [str(tmp_path / "a [emoji \U0001f577️].mkv"), str(tmp_path / "b.jpg")]
    assert host.read_saved(prov, prov.stat().st_size, tmp_path) == []  # nothing new
    assert host.read_saved(tmp_path / "nope.jsonl", 0, tmp_path) == []
