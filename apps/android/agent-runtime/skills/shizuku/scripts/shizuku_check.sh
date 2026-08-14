#!/system/bin/sh
# Shizuku permission self-check for the Storydex Android package.

VERBOSE=0
FIX=0
for a in "$@"; do
    case "$a" in
        -v) VERBOSE=1 ;;
        --fix) FIX=1 ;;
    esac
done

say() { [ "$VERBOSE" = "1" ] && echo "[diagnostic] $*" >&2; }
state() { echo "SHIZUKU_STATE=$1"; }

APP_ID="${RISH_APPLICATION_ID:-com.storydex.android}"

CANDIDATES="
/data/local/tmp/rish/rish_shizuku.dex
$HOME/rish/rish_shizuku.dex
/sdcard/Download/rish_setup/rish_shizuku.dex
"

find_dex() {
    DEX=""
    for c in $CANDIDATES; do
        if [ -f "$c" ]; then DEX="$c"; break; fi
    done
}
find_dex

SDK=$(/system/bin/getprop ro.build.version.sdk 2>/dev/null)
[ -z "$SDK" ] && SDK=$(sed -n 's/^ro.build.version.sdk=//p' /system/build.prop 2>/dev/null)
[ -z "$SDK" ] && SDK=0

dex_writable() {
    m=$(stat -c %a "$1" 2>/dev/null)
    case "$m" in
        *[2367]*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -n "$DEX" ] && [ "$SDK" -ge 34 ] 2>/dev/null && dex_writable "$DEX"; then
    if [ "$FIX" = "1" ]; then
        say "Writable dex detected ($DEX); attempting repair..."
        mkdir -p "$HOME/rish" 2>/dev/null && \
        cp "$DEX" "$HOME/rish/rish_shizuku.dex" 2>/dev/null && \
        chmod 400 "$HOME/rish/rish_shizuku.dex" 2>/dev/null && \
        find_dex
        if [ -n "$DEX" ] && ! dex_writable "$DEX"; then
            say "Repair succeeded: deployed read-only dex at $DEX"
        else
            state "ENV_MISSING"
            say "Repair failed. Copy the dex to \$HOME/rish and chmod 400 it."
            exit 3
        fi
    else
        state "ENV_MISSING"
        say "Android 14+ requires the dex to be read-only."
        say "Automatic repair: sh shizuku_check.sh --fix"
        exit 3
    fi
fi

if [ -z "$DEX" ]; then
    state "ENV_MISSING"
    say "rish_shizuku.dex was not found in the supported locations."
    exit 3
fi
say "Using rish dex: $DEX"

APP_PROC=""
for p in /system/bin/app_process /system/bin/app_process64 /system/bin/app_process32; do
    [ -x "$p" ] && APP_PROC="$p" && break
done
if [ -z "$APP_PROC" ]; then
    state "ENV_MISSING"
    say "No usable app_process was found."
    exit 3
fi

OUT=$(timeout 15 env RISH_APPLICATION_ID="$APP_ID" "$APP_PROC" \
    -Djava.class.path="$DEX" /system/bin --nice-name=rish \
    rikka.shizuku.shell.ShizukuShellLoader -c "echo __SHIZUKU_PROBE__; id" 2>&1)
RC=$?

if [ $RC -eq 0 ] && echo "$OUT" | grep -q "__SHIZUKU_PROBE__"; then
    state "AVAILABLE"
    say "The end-to-end Shizuku probe succeeded."
    echo "$OUT" | grep -E '^(uid|Uid)=' | sed 's/^/    /' >&2
    exit 0
fi

if [ $RC -eq 124 ]; then
    state "UNKNOWN"
    say "The rish call timed out after 15 seconds."
    exit 4
fi

LOWER=$(echo "$OUT" | tr 'A-Z' 'a-z')
case "$LOWER" in
    *"cannot connect"*|*"failed to connect"*|*"unable to get"*|*"not running"*|*"no shizuku"*|*"service not"*|*"server is not"*|*"no such service"*)
        state "SERVER_NOT_RUNNING"
        say "Shizuku server is not running. Start it in the Shizuku app and retry."
        exit 1
        ;;
    *"permission"*|*"denied"*|*"not granted"*|*"securityexception"*)
        state "NOT_GRANTED"
        say "Shizuku is reachable but $APP_ID is not authorized."
        exit 2
        ;;
    *)
        state "UNKNOWN"
        say "Unrecognized error (exit=$RC):"
        echo "$OUT" | sed 's/^/    /' >&2
        exit 4
        ;;
esac
