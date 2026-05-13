#!/usr/bin/env bash
#
# lib/toml.sh — minimal, robust TOML parser for a strict subset.
#
# Supported:
#   - [section.name]
#   - key = "value" / key = 'value' / key = value
#   - key = true / key = 123
#   - key = [ "val1", "val2" ] (multiline or single line)
#   - Comments (#) and whitespace trimming.
#
# Unsupported:
#   - Inline tables { a = 1 }.
#   - Escaped TOML string semantics beyond preserving literal content.
#   - Arrays with quoted commas inside individual items.

toml_trim() {
  local var="$*"
  var="${var#"${var%%[![:space:]]*}"}"
  var="${var%"${var##*[![:space:]]}"}"
  printf '%s' "$var"
}

toml_unquote() {
  local val
  val="$(toml_trim "$1")"
  case "$val" in
    \"*\")
      val="${val#\"}"
      val="${val%\"}"
      ;;
    \'*\')
      val="${val#\'}"
      val="${val%\'}"
      ;;
  esac
  printf '%s' "$val"
}

toml_strip_comment() {
  local line="$1"
  local out=""
  local char
  local in_single=false
  local in_double=false
  local len="${#line}"
  local i=0

  while [ "$i" -lt "$len" ]; do
    char="${line:$i:1}"

    if [ "$in_double" = true ] && [ "$char" = "\\" ]; then
      out="$out$char"
      i=$((i + 1))
      if [ "$i" -lt "$len" ]; then
        char="${line:$i:1}"
        out="$out$char"
      fi
      i=$((i + 1))
      continue
    fi

    if [ "$char" = '"' ] && [ "$in_single" = false ]; then
      if [ "$in_double" = true ]; then
        in_double=false
      else
        in_double=true
      fi
    elif [ "$char" = "'" ] && [ "$in_double" = false ]; then
      if [ "$in_single" = true ]; then
        in_single=false
      else
        in_single=true
      fi
    elif [ "$char" = "#" ] && [ "$in_single" = false ] && [ "$in_double" = false ]; then
      break
    fi

    out="$out$char"
    i=$((i + 1))
  done

  printf '%s' "$out"
}

# toml_parse <file> <callback_function>
# The callback is called as: callback "<section>" "<key>" "<value>"
# For arrays, value is the raw bracketed array string; callers can pass it to
# toml_normalize_array when they need comma-separated shell values.
toml_parse() {
  local config_file="$1"
  local callback="$2"

  [ ! -f "$config_file" ] && return 0

  local current_section=""
  local in_array=false
  local array_key=""
  local array_buffer=""

  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    local line
    line="$(toml_trim "$(toml_strip_comment "$raw_line")")"
    [ -z "$line" ] && continue

    # Section headers
    if [[ "$line" == "["*"]" ]]; then
      current_section="${line#\[}"
      current_section="${current_section%\]}"
      current_section="$(toml_trim "$current_section")"
      in_array=false
      continue
    fi

    # Multiline array continuation
    if [ "$in_array" = true ]; then
      if [[ "$line" == *"]"* ]]; then
        array_buffer="${array_buffer} ${line%%]*}"
        "$callback" "$current_section" "$array_key" "[$array_buffer]"
        in_array=false
        array_buffer=""
      else
        array_buffer="${array_buffer} ${line}"
      fi
      continue
    fi

    # Key-value pairs
    if [[ "$line" == *"="* ]]; then
      local key val
      key="$(toml_trim "${line%%=*}")"
      val="$(toml_trim "${line#*=}")"

      # Array detection
      if [[ "$val" == "["* ]]; then
        if [[ "$val" == *"]" ]]; then
          # Single-line array
          "$callback" "$current_section" "$key" "$val"
        else
          # Start of multiline array
          in_array=true
          array_key="$key"
          array_buffer="${val#\[}"
        fi
      else
        "$callback" "$current_section" "$key" "$(toml_unquote "$val")"
      fi
    fi
  done <"$config_file"
}

# Helper to normalize arrays: strips brackets, quotes, and extra whitespace, returns CSV
toml_normalize_array() {
  local val="$1"
  local item
  val="${val#\[}"
  val="${val%\]}"
  # Split by comma, unquote each, rejoin with comma
  local IFS=','
  local result=""
  for item in $val; do
    item="$(toml_unquote "$(toml_trim "$item")")"
    [ -z "$item" ] && continue
    result="${result}${result:+,}$item"
  done
  printf '%s' "$result"
}
