from src.pipeline.process_csv import process_file


def test_pipeline_dry_run(sample_csv, tmp_path):
    out = tmp_path / "out.csv"
    summary = process_file(
        input_csv=sample_csv,
        output_csv=out,
        target_locale="fr-FR",
        dnt_config_path=None,
        preserve_handle=False,
        resume=False,
        force=True,
        dry_run=True,
        stats=False,
    )
    assert out.exists()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + 10  # header + righe di sample
    assert "translated_rows" in summary
