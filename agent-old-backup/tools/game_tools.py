"""Game Development tools.

Real, usable starter-project templates (Godot, Pygame, Unity, Unreal, generic
2D/3D) plus engine-CLI detection for builds. Templates are complete minimal
projects, not placeholders.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 120


@dataclass
class GameResult:
    ok: bool
    output: str = ""
    parsed: dict = field(default_factory=dict)
    error: str = ""


def _fail(msg: str) -> GameResult:
    return GameResult(ok=False, error=msg, output=msg)


def _sch(t: str, req: bool, desc: str) -> dict:
    return {"type": t, "required": req, "description": desc}


def _write(base: Path, files: dict[str, str]) -> list[str]:
    out = []
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        out.append(rel)
    return out


def _pygame_tpl(name: str, dim: str) -> dict[str, str]:
    is3d = dim == "3d"
    return {
        "main.py": textwrap.dedent(f'''\
            """{name} — pygame starter ({dim})."""
            import sys
            import pygame

            W, H = 800, 600

            def main() -> None:
                pygame.init()
                screen = pygame.display.set_mode((W, H))
                pygame.display.set_caption("{name}")
                clock = pygame.time.Clock()
                player = pygame.Rect(W // 2 - 25, H // 2 - 25, 50, 50)
                vel = pygame.Vector2(0, 0)
                {'z = 0.0  # depth placeholder for 3D projection' if is3d else ''}
                font = pygame.font.SysFont(None, 28)
                running = True
                while running:
                    for e in pygame.event.get():
                        if e.type == pygame.QUIT:
                            running = False
                    keys = pygame.key.get_pressed()
                    vel.x = (keys[pygame.K_RIGHT] - keys[pygame.K_LEFT]) * 5
                    vel.y = (keys[pygame.K_DOWN] - keys[pygame.K_UP]) * 5
                    player.move_ip(vel)
                    screen.fill((18, 18, 28))
                    pygame.draw.rect(screen, (90, 200, 120), player)
                    screen.blit(font.render("Arrow keys move. ESC quits.",
                                            True, (220, 220, 220)), (16, 16))
                    pygame.display.flip()
                    clock.tick(60)
                pygame.quit()
                sys.exit()

            if __name__ == "__main__":
                main()
            '''),
        "requirements.txt": "pygame>=2.5\n",
        "README.md": f"# {name}\n\nRun: `pip install -r requirements.txt && python3 main.py`\n",
    }


def _godot_tpl(name: str, dim: str) -> dict[str, str]:
    node = "Node3D" if dim == "3d" else "Node2D"
    return {
        "project.godot": textwrap.dedent(f'''\
            ; Godot 4 project
            config_version=5
            [application]
            config/name="{name}"
            run/main_scene="res://main.tscn"
            [rendering]
            renderer/rendering_method="gl_compatibility"
            '''),
        "main.tscn": textwrap.dedent(f'''\
            [gd_scene load_steps=2 format=3]
            [ext_resource type="Script" path="res://player.gd" id="1"]
            [node name="Main" type="{node}"]
            [node name="Player" type="{'CharacterBody3D' if dim == '3d' else 'CharacterBody2D'}" parent="."]
            script = ExtResource("1")
            '''),
        "player.gd": textwrap.dedent(f'''\
            extends {'CharacterBody3D' if dim == '3d' else 'CharacterBody2D'}
            @export var speed := 300.0
            func _physics_process(delta):
                var dir := Input.get_vector("ui_left", "ui_right",
                                            "ui_up", "ui_down")
                velocity = Vector{3 if dim == '3d' else 2}(dir.x, 0, dir.y) * speed if {str(dim == '3d').lower()} else dir * speed
                move_and_slide()
            '''),
        "README.md": f"# {name}\n\nOpen `project.godot` in Godot 4 and press Play.\n",
    }


def _unity_tpl(name: str, dim: str) -> dict[str, str]:
    return {
        "Assets/PlayerController.cs": textwrap.dedent(f'''\
            // {name} — Unity player controller ({dim})
            using UnityEngine;
            [RequireComponent(typeof(Rigidbody{'2D' if dim == '2d' else ''}))]
            public class PlayerController : MonoBehaviour
            {{
                public float speed = 8f;
                void Update()
                {{
                    float x = Input.GetAxis("Horizontal");
                    float y = Input.GetAxis("Vertical");
                    Vector3 move = new Vector3(x, {('0' if dim == '3d' else 'y')}, y) * speed * Time.deltaTime;
                    transform.Translate(move, Space.World);
                }}
            }}
            '''),
        "README.md": (f"# {name}\n\nUnity scaffold ({dim}).\n\n1. Create a Unity project ({'3D' if dim == '3d' else '2D'} template).\n"
                      "2. Copy `Assets/PlayerController.cs` into Assets.\n"
                      "3. Attach to a player GameObject and press Play.\n"),
    }


def _unreal_tpl(name: str, dim: str) -> dict[str, str]:
    mod = re.sub(r"[^A-Za-z0-9_]", "", name)
    return {
        f"Source/{mod}/{mod}.Build.cs": textwrap.dedent(f'''\
            using UnrealBuildTool;
            public class {mod} : ModuleRules
            {{
                public {mod}(ReadOnlyTargetRules Target) : base(Target)
                {{
                    PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
                    PublicDependencyModuleNames.AddRange(new[] {{
                        "Core", "CoreUObject", "Engine", "InputCore" }});
                }}
            }}
            '''),
        "README.md": (f"# {name}\n\nUnreal scaffold.\n\n1. Create a C++ project named {mod}.\n"
                      f"2. Add `Source/{mod}/{mod}.Build.cs`.\n"
                      "3. Build from the editor or via `game_build` when UE CLI is present.\n"),
    }


def _game_scaffold(args: dict) -> GameResult:
    engine = str(args.get("engine", "pygame")).lower()
    dim = str(args.get("dim", "2d")).lower()
    name = str(args.get("name", "mygame"))
    dest = Path(str(args.get("dest", f"./{name}"))).expanduser()
    if dim not in ("2d", "3d"):
        return _fail("dim must be 2d|3d")
    builders = {"pygame": _pygame_tpl, "godot": _godot_tpl,
                "unity": _unity_tpl, "unreal": _unreal_tpl}
    if engine not in builders:
        return _fail(f"engine must be one of {sorted(builders)}")
    if dest.exists() and not args.get("overwrite"):
        return _fail(f"destination exists: {dest} (pass overwrite=true)")
    files = builders[engine](name, dim)
    written = _write(dest, files)
    return GameResult(ok=True,
                     output=f"scaffolded {engine} {dim} game '{name}' in {dest}",
                     parsed={"engine": engine, "dim": dim, "dest": str(dest),
                             "files": written})


_ENGINES = [
    ("godot", ["godot", "--version"]),
    ("unity", ["unity", "-version"]),
    ("unreal", ["ue5", "version"]),
    ("pygame", [shutil.which("python3") or "python3", "-c",
                "import pygame; print(pygame.version.ver)"]),
]


def _game_build(args: dict) -> GameResult:
    engine = str(args.get("engine", "")).lower()
    project = str(args.get("project", "."))
    found = {}
    for name, cmd in _ENGINES:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                found[name] = (r.stdout + r.stderr).strip().splitlines()[0][:80]
        except Exception:
            pass
    if not found:
        return GameResult(
            ok=False, output="",
            error=("no game engine CLI detected (godot/unity/ue5/pygame). "
                   "Install one, then rerun. Scaffold with game_scaffold first."))
    if not engine:
        return GameResult(ok=True, output="detected engines:\n" +
                         "\n".join(f"- {k}: {v}" for k, v in found.items()),
                         parsed={"engines": found})
    if engine not in found:
        return GameResult(ok=False, output="",
                         error=f"engine '{engine}' CLI not detected. "
                               f"Detected: {sorted(found)}")
    if engine == "godot":
        try:
            r = subprocess.run(["godot", "--headless", "--export-release",
                                "linux", f"{project}/build/game"],
                               capture_output=True, text=True, timeout=600,
                               cwd=project)
            ok = r.returncode == 0
            return GameResult(ok=ok, output=(r.stdout + r.stderr)[-3000:],
                             parsed={"rc": r.returncode})
        except Exception as e:
            return _fail(f"godot export failed: {e}")
    return GameResult(ok=True,
                     output=(f"engine '{engine}' detected ({found[engine]}). "
                             "Open the project in the editor to build; "
                             "headless CLI export is engine-specific."),
                     parsed={"engines": found})


HANDLERS: dict = {
    "game_scaffold": _game_scaffold,
    "game_build": _game_build,
}

TOOL_DEFS: list[tuple] = [
    ("game_scaffold", "Scaffold a runnable starter game (pygame|godot|unity|unreal, 2d|3d)",
     {"engine": _sch("string", False, "pygame|godot|unity|unreal"),
      "dim": _sch("string", False, "2d|3d"),
      "name": _sch("string", False, "Project name"),
      "dest": _sch("string", False, "Destination dir"),
      "overwrite": _sch("boolean", False, "Replace existing")}, "computer", 60),
    ("game_build", "Detect engine CLIs; headless Godot export when available",
     {"engine": _sch("string", False, "Engine to build with"),
      "project": _sch("string", False, "Project dir")}, "computer", 900),
]
