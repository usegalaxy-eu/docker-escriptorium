These two unmodified text images come from eScriptorium **v26.10**, commit
`51b2ee62c307bd13eb0f6bad894f124eee7265cc`:

- [default.png](https://gitlab.com/scripta/escriptorium/-/blob/51b2ee62c307bd13eb0f6bad894f124eee7265cc/app/apps/core/tests/assets/segmentation/default.png): Hebrew print, 864 × 206 pixels.
- [default2.png](https://gitlab.com/scripta/escriptorium/-/blob/51b2ee62c307bd13eb0f6bad894f124eee7265cc/app/apps/core/tests/assets/segmentation/default2.png): Latin handwriting, 747 × 212 pixels.

The upstream repository's MIT license is included in `LICENSE`.

`fake_seg.mlmodel` is also copied from the same upstream version, at
`app/apps/core/tests/assets/fake_seg.mlmodel`, for segmentation-model imports.

`tests/fixtures.py` imports the first as `01-page.png` and packages the second
as `page2.png` in a ZIP. The accompanying ALTO XML uses each image's actual
dimensions and synthetic text markers to verify import/export; it does not
claim to transcribe the pictured text. No network downloads are needed in tests.

SHA-256 checksums:

```text
6a226ff5ed9dc78140e622cb0867c548cc02909565fa0eb8f1680728a43a2ee3  default.png
2aa88e9eefe31032d670394964fd0510289c8722c796a455ce0d3dc72f70184e  default2.png
```

The baseline recognition fixture comes from Kraken **7.1.1**, commit
`a314f84f42da49a34079a4166305a7f378bb9c71`:

- [overfit_bl.safetensors](https://github.com/mittagessen/kraken/blob/a314f84f42da49a34079a4166305a7f378bb9c71/tests/resources/overfit_bl.safetensors): baseline recognition model, 113,632 bytes, used by Kraken's polygon-extraction tests.
- `overfit_bl_coreml.mlmodel`: the same model converted once to CoreML with the
  container's Kraken 7.1.1. Tensor values and codec were verified identical after
  reloading. This is a format conversion, not a newly generated or trained model.

The upstream `overfit.mlmodel` fixture uses bounding-box segmentation, which
eScriptorium's upload form rejects. Both fixtures used here declare `baselines`.
To reproduce the conversion with the built image, run from the repository root:

```bash
docker run --rm --network none --entrypoint python3 \
  -v "$PWD/tests/assets:/fixtures" quay.io/galaxy/escriptorium:26.10 \
  -c 'from kraken.tasks import RecognitionTaskModel; RecognitionTaskModel.load_model("/fixtures/overfit_bl.safetensors").net.save_model("/fixtures/overfit_bl_coreml.mlmodel")'
```

Kraken's Apache-2.0 license is included in `LICENSE.kraken`. These deliberately
small test fixtures exercise recognition-model detection, import, export and
restoration in both formats. They are not general-purpose OCR models, and the
integration suite does not assert transcription accuracy. Copying the fixtures
also avoids launching a separate container to generate random model weights
for each test instance. Their compatibility is verified with the container's
installed Kraken version.

SHA-256 checksums:

```text
92654ab900547d3eae1645fe79ef5f2d48f95613240b7ca782542fb2c9eec7e5  overfit_bl_coreml.mlmodel
f9e786501c0de862f71ea2e576d7064cf012a8f8d35bc14e257fa550c3fef1d5  overfit_bl.safetensors
```
