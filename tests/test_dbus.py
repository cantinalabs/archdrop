from archdrop import dbus


def test_marshal_roundtrip_of_portal_shapes():
    w = dbus.Writer()
    for sig, value in (("s", "héllo"), ("o", "/a/b"), ("u", 7), ("b", True),
                       ("a{sv}", {"uris": ("as", ["file:///x", "file:///y"]), "n": ("u", 3)}),
                       ("(ua{sv})", (0, {"k": ("s", "v")})), ("ay", [0, 255])):
        w = dbus.Writer()
        w.write("y", 1)                       # misalign on purpose
        w.write(sig, value)
        r = dbus.Reader(bytes(w.buf))
        r.read("y")
        got = r.read(sig)
        expect = value
        if sig == "a{sv}":
            expect = {k: v[1] for k, v in value.items()}
        if sig == "(ua{sv})":
            expect = (0, {"k": "v"})
        assert got == expect, sig


def test_signature_splitting():
    assert dbus._split("ssa{sv}(ua{sv})as") == ["s", "s", "a{sv}", "(ua{sv})", "as"]
