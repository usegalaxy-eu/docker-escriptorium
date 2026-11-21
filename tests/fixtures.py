#!/usr/bin/env python3
"""
Shared fixture generator for the docker-escriptorium tests.

Creates three importable files in the given directory:

  01-page.png             a small generated PNG (becomes a document part)
  02-transcriptions.xml   an ALTO XML bound to 01-page.png (2 lines with
                          transcriptions)
  03-export.zip           an eScriptorium-export-style archive containing
                          page2.png + page2.xml (second part + 1 line)

Usage:  python3 fixtures.py <target-dir>
"""
import os
import struct
import sys
import zipfile
import zlib

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
    <Page WIDTH="200" HEIGHT="100" PHYSICAL_IMG_NR="0" ID="page_0">
      <PrintSpace HPOS="0" VPOS="0" WIDTH="200" HEIGHT="100">
        <TextBlock HPOS="10" VPOS="10" ID="textblock_0" WIDTH="180" HEIGHT="{block_height}">
{lines}
        </TextBlock>
      </PrintSpace>
    </Page>
  </Layout>
</alto>
"""

LINE_TEMPLATE = """          <TextLine ID="line_{i}" HPOS="10" VPOS="{vpos}" WIDTH="180" HEIGHT="20">
            <String ID="segment_{i}" CONTENT="{content}" HPOS="10" VPOS="{vpos}" WIDTH="180" HEIGHT="20" WC="0.99"></String>
          </TextLine>"""


def write_png(path, w, h, color):
    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(h))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(png)


def alto_xml(image_name, lines):
    body = "\n".join(
        LINE_TEMPLATE.format(i=i, vpos=10 + i * 30, content=content)
        for i, content in enumerate(lines)
    )
    return ALTO_TEMPLATE.format(
        image=image_name, block_height=len(lines) * 30, lines=body
    )


def generate(target):
    os.makedirs(target, exist_ok=True)

    write_png(os.path.join(target, "01-page.png"), 200, 100, (255, 255, 255))
    with open(os.path.join(target, "02-transcriptions.xml"), "w") as fh:
        fh.write(alto_xml("01-page.png", ["Hello world", "second line"]))

    # second page, packaged as an eScriptorium-export-style zip
    write_png(os.path.join(target, "page2.png"), 200, 100, (240, 240, 255))
    with open(os.path.join(target, "page2.xml"), "w") as fh:
        fh.write(alto_xml("page2.png", ["zipped"]))
    with zipfile.ZipFile(os.path.join(target, "03-export.zip"), "w") as zf:
        zf.write(os.path.join(target, "page2.png"), "page2.png")
        zf.write(os.path.join(target, "page2.xml"), "page2.xml")
    os.remove(os.path.join(target, "page2.png"))
    os.remove(os.path.join(target, "page2.xml"))

    return ["01-page.png", "02-transcriptions.xml", "03-export.zip"]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} <target-dir>")
    for name in generate(sys.argv[1]):
        print(name)
