#!/bin/sh
# Holt den SAM2-Checkpoint beim ersten Start, statt ihn ins Image zu backen.
#
# Das Modell ist 898 MB. Im Image aufgehoben blaeht es jede Kopie des Images auf
# und wird bei jeder Cache-Invalidierung neu geladen; im Git-Repo hat es nichts
# verloren (GitLab-Quota, und *.pt liegt per .gitattributes in git-lfs). Also
# liegt es host-seitig in ./checkpoints (gitignored) und ueberlebt dort Rebuilds.
set -eu

HOST_UID="${HOST_UID:-1000}"
HOST_GID="${HOST_GID:-1000}"

if [ "$(id -u)" = "0" ]; then
    # Bind-mount-Quellen, die auf dem Host noch nicht existierten (auf einem
    # frischen Clone immer der Fall: ./checkpoints und API_STORAGE_DIR/SAM_data
    # sind gitignored) wurden von Docker gerade eben als root angelegt, bevor
    # dieser Container ueberhaupt gestartet ist -- der Laufzeit-User koennte
    # dort nie hineinschreiben. Siehe docker-context-gui/docker-entrypoint.sh
    # fuer denselben Fix. Einmalig chown'en, dann fuer den Rest (inklusive
    # Checkpoint-Download und CMD) zum echten Laufzeit-User wechseln.
    mkdir -p /app/data /app/checkpoints
    chown -R "$HOST_UID:$HOST_GID" /app/data /app/checkpoints
    exec gosu "$HOST_UID:$HOST_GID" "$0" "$@"
fi

CKPT="${SAM2_CHECKPOINT:-/app/checkpoints/sam2.1_hiera_large.pt}"
CKPT_URL="${SAM2_CHECKPOINT_URL:-https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_large.pt}"
CKPT_SHA256="${SAM2_CHECKPOINT_SHA256:-2647878d5dfa5098f2f8649825738a9345572bae2d4350a2468587ece47dd318}"

# Vorhandene Datei wird NICHT jedes Mal geprueft - eine sha256-Summe ueber
# 898 MB kostet Sekunden bei jedem Neustart. Die Integritaet wird beim
# Herunterladen sichergestellt (siehe .part unten).
if [ ! -s "$CKPT" ]; then
    echo "SAM2-Checkpoint fehlt unter $CKPT - lade einmalig herunter (~898 MB)..."
    mkdir -p "$(dirname "$CKPT")"

    # Erst nach .part laden und dann umbenennen: ein abgebrochener Download
    # hinterlaesst sonst eine abgeschnittene Datei, die beim naechsten Start
    # als "vorhanden" durchginge und torch.load zur Laufzeit sprengen wuerde.
    if ! curl -fL --retry 3 --retry-delay 5 -o "$CKPT.part" "$CKPT_URL"; then
        echo "FEHLER: Download fehlgeschlagen ($CKPT_URL)." >&2
        echo "Haengt der Container hinter dem Proxy? HTTPS_PROXY pruefen." >&2
        rm -f "$CKPT.part"
        exit 1
    fi

    echo "Pruefe sha256..."
    if ! echo "$CKPT_SHA256  $CKPT.part" | sha256sum -c - >/dev/null 2>&1; then
        echo "FEHLER: sha256 stimmt nicht. Erwartet: $CKPT_SHA256" >&2
        echo "Erhalten: $(sha256sum "$CKPT.part" | cut -d' ' -f1)" >&2
        rm -f "$CKPT.part"
        exit 1
    fi

    mv "$CKPT.part" "$CKPT"
    echo "Checkpoint bereit: $CKPT"
fi

exec "$@"
