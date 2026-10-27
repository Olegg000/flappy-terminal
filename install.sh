#!/bin/sh
# Flappy Bird for the terminal - installer for macOS and Linux.
#
#   Install / update:  curl -fsSL https://raw.githubusercontent.com/Olegg000/flappy-terminal/main/install.sh | sh
#   Uninstall:         curl -fsSL https://raw.githubusercontent.com/Olegg000/flappy-terminal/main/install.sh | sh -s -- --uninstall
#
# What it does: finds Python 3.9+ with curses, downloads flappy.py to ~/.local/share/flappy-terminal,
# puts a `flappy` command in ~/.local/bin and, if that folder is not on your PATH yet, adds it to your
# shell's startup file.  No sudo, nothing outside your home folder.
set -eu

RAW_URL="${FLAPPY_RAW_URL:-https://raw.githubusercontent.com/Olegg000/flappy-terminal/main}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/flappy-terminal"
BIN_DIR="$HOME/.local/bin"
CMD="$BIN_DIR/flappy"
MARK="# added by flappy-terminal"

if [ -t 1 ]; then
    B="$(printf '\033[1m')"; G="$(printf '\033[32m')"; Y="$(printf '\033[33m')"; R="$(printf '\033[31m')"; N="$(printf '\033[0m')"
else
    B=""; G=""; Y=""; R=""; N=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s+%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

uninstall() {
    step "Removing Flappy Bird"
    rm -f "$CMD"
    rm -rf "$APP_DIR"
    ok "removed $CMD and $APP_DIR"
    say "  Your best scores stay in ~/.flappy_term.json (delete it if you want)."
    say "  The PATH line in your shell startup file is left alone: other tools may use ~/.local/bin."
}

case "${1:-}" in
    --uninstall|uninstall) uninstall; exit 0 ;;
    ""|--install|install) ;;
    *) die "unknown option: $1 (use --uninstall or nothing)" ;;
esac

say ""
say "${B}  Flappy Bird for the terminal - installer${N}"
say ""

# --- 1. Python 3.9+ with curses -------------------------------------------------------------------
step "Looking for Python 3.9+ with curses"
PY=""
for cand in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    path="$(command -v "$cand" 2>/dev/null || true)"
    [ -n "$path" ] || continue
    # keep the PATH entry (/usr/bin/python3, /opt/homebrew/bin/python3): it survives Python upgrades
    if "$path" -c 'import sys, curses; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
        PY="$path"
        break
    fi
done
if [ -z "$PY" ]; then
    warn "no suitable Python found"
    case "$(uname -s)" in
        Darwin) say "  Install one, then run this installer again:"
                say "    brew install python          (https://brew.sh)"
                say "    or: xcode-select --install   (Apple's command line tools include python3)" ;;
        *)      say "  Install Python 3 with your package manager, then run this installer again:"
                say "    Debian/Ubuntu:  sudo apt install python3"
                say "    Fedora:         sudo dnf install python3"
                say "    Arch:           sudo pacman -S python"
                say "    Alpine:         sudo apk add python3"
                say "    openSUSE:       sudo zypper install python3 python3-curses" ;;
    esac
    exit 1
fi
ok "$PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# --- 2. Download the game -------------------------------------------------------------------------
step "Downloading the game"
mkdir -p "$APP_DIR" "$BIN_DIR"
TMP="$APP_DIR/flappy.py.download"
trap 'rm -f "$TMP"' EXIT
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$RAW_URL/flappy.py" -o "$TMP" || die "download failed: $RAW_URL/flappy.py"
elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$TMP" "$RAW_URL/flappy.py" || die "download failed: $RAW_URL/flappy.py"
else
    die "neither curl nor wget is installed"
fi
"$PY" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$TMP" 2>/dev/null \
    || die "the downloaded file is not valid Python (network problem?); nothing was changed"
mv -f "$TMP" "$APP_DIR/flappy.py"
ok "$APP_DIR/flappy.py"

# --- 3. The `flappy` command ----------------------------------------------------------------------
step "Creating the flappy command"
cat > "$CMD" <<EOF
#!/bin/sh
# flappy-terminal launcher (made by install.sh; re-run the installer to update)
PY='$PY'
command -v "\$PY" >/dev/null 2>&1 || PY=python3
exec "\$PY" '$APP_DIR/flappy.py' "\$@"
EOF
chmod +x "$CMD"
ok "$CMD"

# --- 4. PATH --------------------------------------------------------------------------------------
on_path=no
case ":$PATH:" in *":$BIN_DIR:"*) on_path=yes ;; esac
if [ "$on_path" = no ]; then
    step "Adding ~/.local/bin to your PATH"
    shell_name="$(basename "${SHELL:-sh}")"
    case "$shell_name" in
        zsh)  rc="${ZDOTDIR:-$HOME}/.zshrc" ;;
        bash) if [ "$(uname -s)" = Darwin ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
        fish) rc="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/flappy-terminal.fish" ;;
        *)    rc="$HOME/.profile" ;;
    esac
    if [ -f "$rc" ] && grep -q "$MARK" "$rc" 2>/dev/null; then
        ok "already set up in $rc"
    else
        mkdir -p "$(dirname "$rc")"
        # shellcheck disable=SC2016  # $PATH must stay literal: it is expanded by the shell at startup
        if [ "$shell_name" = fish ]; then
            printf '\n%s\nif not contains "%s" $PATH; set -gx PATH "%s" $PATH; end\n' "$MARK" "$BIN_DIR" "$BIN_DIR" >> "$rc"
        else
            printf '\n%s\nexport PATH="$HOME/.local/bin:$PATH"\n' "$MARK" >> "$rc"
        fi
        ok "added to $rc"
    fi
fi

# --- Done -----------------------------------------------------------------------------------------
say ""
say "${G}${B}  Installed!${N}"
say ""
if [ "$on_path" = yes ]; then
    say "  Start the game:   ${B}flappy${N}"
else
    say "  Open a new terminal window and type:   ${B}flappy${N}"
    say "  (or, in this window:  ${B}export PATH=\"\$HOME/.local/bin:\$PATH\" && flappy${N})"
fi
say ""
say "  Space / Up = flap   P = pause   Q = quit   flappy --help for options"
say "  Update: run the same install command again.   Uninstall: add  -s -- --uninstall  after sh"
say ""
