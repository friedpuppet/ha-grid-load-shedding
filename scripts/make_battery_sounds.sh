#!/usr/bin/env bash
# Generate the battery notification sounds: a short neutral chime, then the voice.
# Same voice and rate as grid_lost_ostap_6.mp3, same loudness (-19.6 LUFS).
# Needs uv (for edge-tts) and ffmpeg. Usage: scripts/make_battery_sounds.sh [out_dir]
set -euo pipefail

out=${1:-sounds/battery-alpha}
voice=uk-UA-OstapNeural
rate=+10%
lufs=-18.7  # one-pass loudnorm lands about 0.9 LU under the target
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"

# One soft strike: a fifth above a mid tone, fading out. Neutral on purpose, so it differs
# from the dark tritone of "grid lost".
strike() { # freq_a freq_b
    ffmpeg -loglevel error -y -f lavfi \
        -i "aevalsrc='0.5*(sin(2*PI*$1*t)+0.4*sin(2*PI*$2*t))*exp(-5*t)':d=0.6:s=24000:c=mono" "$3"
}
strike 660 990 "$work/a.wav"
strike 880 1320 "$work/b.wav"
ffmpeg -loglevel error -y -i "$work/a.wav" -i "$work/b.wav" \
    -filter_complex "[1]adelay=220[d];[0][d]amix=2:duration=longest:normalize=0,apad=pad_dur=0.25" \
    "$work/chime.wav"

make() { # name text
    uvx edge-tts --voice "$voice" --rate="$rate" --text "$2" --write-media "$work/voice.mp3" >/dev/null
    ffmpeg -loglevel error -y -i "$work/chime.wav" -i "$work/voice.mp3" -filter_complex \
        "[1]aresample=24000,aformat=channel_layouts=mono[v];[0][v]concat=n=2:v=0:a=1,loudnorm=I=$lufs:TP=-1:LRA=11,aresample=24000" \
        -b:a 64k "$out/$1.mp3"
}

make battery_soc_50_ostap "У батареї лишилась половина заряду."
make battery_soc_25_ostap "У батареї лишилась чверть заряду."
make battery_runtime_120_ostap "Батареї вистачить на дві години."
make battery_runtime_60_ostap "Батареї вистачить на одну годину."
make battery_runtime_30_ostap "Батареї вистачить на пів години."
make battery_runtime_10_ostap "Батареї вистачить на десять хвилин."
