#!/usr/bin/env python3
"""
Shared fixture generator for the docker-escriptorium tests.

Creates three importable files in the given directory:

  01-page.png             upstream Hebrew print image (becomes a document part)
  02-transcriptions.xml   an ALTO XML bound to 01-page.png (2 lines with
                          transcriptions)
  03-export.zip           an eScriptorium-export-style archive containing
                          upstream handwriting image + XML (second part + 1 line)

The images are unchanged upstream segmentation fixtures (see assets/README.md).
The XML contains test markers, not ground-truth transcriptions of these images.

Usage:  python3 fixtures.py <target-dir>
"""

import os
from pathlib import Path
import shutil
import struct
import sys
import zipfile

ALTO_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<alto xmlns="http://www.loc.gov/standards/alto/ns-v4#"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xsi:schemaLocation="http://www.loc.gov/standards/alto/ns-v4# http://www.loc.gov/standards/alto/v4/alto-4-0.xsd">
  <Description>
    <sourceImageInformation>
      <fileName>{image}</fileName>
    </sourceImageInformation>
  </Description>
  <Layout>
    <Page WIDTH="{width}" HEIGHT="{height}" PHYSICAL_IMG_NR="0" ID="page_0">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="{width}" HEIGHT="{height}">
        <TextBlock HPOS="10" VPOS="10" ID="textblock_0" WIDTH="{line_width}" HEIGHT="{block_height}">
{lines}
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>
"""

LINE_TEMPLATE = """          <TextLine ID="line_{i}" HPOS="10" VPOS="{vpos}" WIDTH="{line_width}" HEIGHT="{line_height}">
            <String ID="segment_{i}" CONTENT="{content}" HPOS="10" VPOS="{vpos}" WIDTH="{line_width}" HEIGHT="{line_height}" WC="0.99"></String>
          </TextLine>"""


def alto_xml(image_name, lines, image_path):
    # Read the PNG dimensions without adding a host-side imaging dependency.
    width, height = struct.unpack(">II", Path(image_path).read_bytes()[16:24])
    line_width, line_height, spacing = width - 20, height // 5, height // 3
    body = "\n".join(
        LINE_TEMPLATE.format(
            i=i,
            vpos=10 + i * spacing,
            content=content,
            line_width=line_width,
            line_height=line_height,
        )
        for i, content in enumerate(lines)
    )
    return ALTO_TEMPLATE.format(
        image=image_name,
        width=width,
        height=height,
        line_width=line_width,
        block_height=(len(lines) - 1) * spacing + line_height,
        lines=body,
    )


def generate(target):
    os.makedirs(target, exist_ok=True)

    assets = Path(__file__).parent / "assets"
    shutil.copyfile(assets / "default.png", os.path.join(target, "01-page.png"))
    with open(os.path.join(target, "02-transcriptions.xml"), "w") as fh:
        fh.write(
            alto_xml(
                "01-page.png", ["Hello world", "second line"], assets / "default.png"
            )
        )

    # second page, packaged as an eScriptorium-export-style zip
    with zipfile.ZipFile(os.path.join(target, "03-export.zip"), "w") as zf:
        zf.write(assets / "default2.png", "page2.png")
        zf.writestr(
            "page2.xml", alto_xml("page2.png", ["zipped"], assets / "default2.png")
        )

    return ["01-page.png", "02-transcriptions.xml", "03-export.zip"]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <target-dir>")
    for name in generate(sys.argv[1]):
        print(name)
