from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import CutRange


@dataclass(frozen=True)
class AudioManifest:
    input_path: Path
    output_path: Path
    ffmpeg_command: tuple[str, ...]
    cuts: tuple[CutRange, ...]
    normalize: bool
    target_format: str

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "input_path": str(self.input_path),
            "output_path": str(self.output_path),
            "ffmpeg_command": list(self.ffmpeg_command),
            "cuts": [asdict(cut) for cut in self.cuts],
            "normalize": self.normalize,
            "target_format": self.target_format,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path


def build_prepare_command(
    input_path: Path,
    output_path: Path,
    *,
    cuts: tuple[CutRange, ...] = (),
    normalize: bool = True,
    sample_rate: int = 44_100,
    bitrate: str = "192k",
) -> tuple[str, ...]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command: list[str] = ["ffmpeg", "-hide_banner", "-y", "-i", str(input_path)]
    filters: list[str] = []
    if cuts:
        filters.append(_aselect_filter(cuts))
    if normalize:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if filters:
        command.extend(["-af", ",".join(filters)])
    command.extend(["-ar", str(sample_rate), "-b:a", bitrate, "-f", "mp3", str(output_path)])
    return tuple(command)


def build_manifest(input_path: Path, output_path: Path, cuts: tuple[CutRange, ...] = (), normalize: bool = True) -> AudioManifest:
    return AudioManifest(
        input_path=input_path,
        output_path=output_path,
        ffmpeg_command=build_prepare_command(input_path, output_path, cuts=cuts, normalize=normalize),
        cuts=cuts,
        normalize=normalize,
        target_format="mp3",
    )


def shell_join(command: tuple[str, ...]) -> str:
    return shlex.join(command)


def _aselect_filter(cuts: tuple[CutRange, ...]) -> str:
    sorted_cuts = sorted(cuts, key=lambda cut: cut.start_sec)
    expressions = [f"not(between(t,{cut.start_sec:.6f},{cut.end_sec:.6f}))" for cut in sorted_cuts]
    return "aselect='" + "*".join(expressions) + "',asetpts=N/SR/TB"
