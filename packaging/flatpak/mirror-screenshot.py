import gzip
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


mode, target, screenshot, media, timestamp = sys.argv[1:]
if mode == "catalog":
    path = (
        Path(target)
        / "files/share/app-info/xmls/network.katzenpost.katzenqt.xml.gz"
    )
    root = ET.fromstring(gzip.decompress(path.read_bytes()))
    component = root.find("component")
    screenshots = component.find("screenshots")
    if screenshots is None:
        screenshots = ET.SubElement(component, "screenshots")
    images = screenshots.findall("./screenshot/image")
    if not images:
        screenshot_node = ET.SubElement(
            screenshots, "screenshot", {"type": "default"}
        )
        images = [ET.SubElement(screenshot_node, "image")]
        ET.SubElement(
            screenshot_node, "caption"
        ).text = "katzenqt conversation window"
    for image in images:
        image.text = media
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    path.write_bytes(gzip.compress(data, mtime=0))
else:
    with tempfile.TemporaryDirectory() as directory:
        shutil.copy2(screenshot, Path(directory) / Path(media).name)
        subprocess.run(
            [
                "ostree",
                "commit",
                f"--repo={target}",
                "--branch=screenshots/"
                + subprocess.check_output(
                    ["flatpak", "--default-arch"], text=True
                ).strip(),
                f"--tree=dir={directory}",
                f"--timestamp={timestamp}",
                "--canonical-permissions",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
