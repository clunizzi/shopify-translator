from src.state import neon


def test_default_sslrootcert_prefers_env(monkeypatch):
    monkeypatch.setenv("PGSSLROOTCERT", "/custom/ca.pem")
    assert neon.default_sslrootcert() == "/custom/ca.pem"


def test_default_sslrootcert_finds_known_bundle(monkeypatch):
    monkeypatch.delenv("PGSSLROOTCERT", raising=False)
    monkeypatch.setattr(neon.Path, "exists", lambda self: str(self) == "/etc/pki/tls/certs/ca-bundle.crt")
    assert neon.default_sslrootcert() == "/etc/pki/tls/certs/ca-bundle.crt"
