# bash completion for quaderno: subcommands, options, local files, and paths on
# the device (fetched live via `quaderno __complete`, ~0.4 s per Tab).
#
# Install (automatic with bash-completion on Linux):
#   mkdir -p ~/.local/share/bash-completion/completions
#   ln -sf ~/quaderno/completions/quaderno.bash ~/.local/share/bash-completion/completions/quaderno
#
# Or source directly from ~/.bashrc:
#   source ~/quaderno/completions/quaderno.bash

_quaderno_unescape() {
    local s=$1 out="" i=0 n=${#1} ch
    while (( i < n )); do
        ch=${s:i:1}
        if [[ $ch == '\' && $((i + 1)) -lt n ]]; then
            out+=${s:i+1:1}
            ((i += 2))
        else
            out+=$ch
            ((i += 1))
        fi
    done
    printf '%s' "$out"
}

_quaderno_device_paths() {
    local kind=$1 cur=$2
    local quote="" typed=$cur

    if [[ $cur == \'* ]]; then
        quote="'"
        typed=${cur#\'}
    elif [[ $cur == \"* ]]; then
        quote='"'
        typed=${cur#\"}
    else
        typed=$(_quaderno_unescape "$cur")
    fi

    # Glob pattern: leave untouched
    case "$typed" in
        *'*'*|*'?'*|*'['*) return 0 ;;
    esac

    local -a flags=()
    [[ $kind == dirs ]] && flags+=(--dirs)
    [[ -n $_quaderno_config ]] && flags+=(--config "$_quaderno_config")
    [[ -n $_quaderno_addr ]] && flags+=(--addr "$_quaderno_addr")
    [[ -n $_quaderno_transport ]] && flags+=("$_quaderno_transport")

    local -a raw_lines=()
    local line
    while IFS= read -r line; do
        [[ -n $line ]] && raw_lines+=("$line")
    done < <(quaderno __complete "${flags[@]}" -- "$typed" 2>/dev/null)

    (( ${#raw_lines[@]} == 0 )) && return 0

    local had_nocasematch=0
    shopt -q nocasematch && had_nocasematch=1
    shopt -s nocasematch

    local -a matches=()
    local has_dir=0 has_file=0 formatted escaped
    for line in "${raw_lines[@]}"; do
        if [[ $line == "$typed"* ]]; then
            if [[ $line == */ ]]; then
                has_dir=1
            else
                has_file=1
            fi
            if [[ $quote == "'" ]]; then
                formatted="'${line}"
            elif [[ $quote == '"' ]]; then
                escaped=${line//\\/\\\\}
                escaped=${escaped//\"/\\\"}
                escaped=${escaped//\$/\\\$}
                escaped=${escaped//\`/\\\`}
                formatted="\"${escaped}"
            else
                printf -v formatted '%q' "$line"
            fi
            matches+=("$formatted")
        fi
    done

    (( had_nocasematch == 0 )) && shopt -u nocasematch

    COMPREPLY=("${matches[@]}")
    if (( has_dir == 1 && has_file == 0 )) || [[ ${#matches[@]} -eq 1 && ${matches[0]} == */ ]]; then
        compopt -o nospace 2>/dev/null || true
    fi
}

_quaderno_local_files() {
    local cur=$1 kind=${2:-file}
    local IFS=$'\n'
    if [[ $kind == dir ]]; then
        COMPREPLY=($(compgen -d -- "$cur"))
        compopt -o filenames -o nospace 2>/dev/null || true
    elif [[ $kind == pdf ]]; then
        COMPREPLY=($(compgen -f -X '!*.[pP][dD][fF]' -- "$cur") $(compgen -d -- "$cur"))
        compopt -o filenames 2>/dev/null || true
    else
        COMPREPLY=($(compgen -f -- "$cur"))
        compopt -o filenames 2>/dev/null || true
    fi
}

_quaderno() {
    COMPREPLY=()
    local cur=${COMP_WORDS[COMP_CWORD]}
    local prev=""
    (( COMP_CWORD > 0 )) && prev=${COMP_WORDS[COMP_CWORD - 1]}

    local global_opts="-h --help --addr --wifi --usb --config -v --verbose"
    local cmds="ls get put mkdir mv rm sync open search cp viewing marks ping replace annotations templates wifi power firmware pen-buttons certs pastlog config time info screenshot addr pair linux-setup"

    # Scan global options before the subcommand
    local _quaderno_config="" _quaderno_addr="" _quaderno_transport=""
    local cmd="" cmd_idx=0 i=1 w
    while (( i < COMP_CWORD )); do
        w=${COMP_WORDS[i]}
        case "$w" in
            --config)
                (( i + 1 < COMP_CWORD )) && _quaderno_config=${COMP_WORDS[i + 1]}
                (( i += 2 ))
                continue
                ;;
            --config=*)
                _quaderno_config=${w#--config=}
                (( i += 1 ))
                continue
                ;;
            --addr)
                (( i + 1 < COMP_CWORD )) && _quaderno_addr=${COMP_WORDS[i + 1]}
                (( i += 2 ))
                continue
                ;;
            --addr=*)
                _quaderno_addr=${w#--addr=}
                (( i += 1 ))
                continue
                ;;
            --usb|--wifi)
                _quaderno_transport=$w
                (( i += 1 ))
                continue
                ;;
            -v|--verbose|-h|--help)
                (( i += 1 ))
                continue
                ;;
            -*)
                (( i += 1 ))
                continue
                ;;
            *)
                cmd=$w
                cmd_idx=$i
                break
                ;;
        esac
    done

    # Strip surrounding quotes from global option values if present
    _quaderno_config=${_quaderno_config#\'}; _quaderno_config=${_quaderno_config%\'}
    _quaderno_config=${_quaderno_config#\"}; _quaderno_config=${_quaderno_config%\"}
    _quaderno_addr=${_quaderno_addr#\'}; _quaderno_addr=${_quaderno_addr%\'}
    _quaderno_addr=${_quaderno_addr#\"}; _quaderno_addr=${_quaderno_addr%\"}

    # Before a subcommand is chosen
    if [[ -z $cmd ]]; then
        case "$prev" in
            --config)
                _quaderno_local_files "$cur" dir
                return 0
                ;;
            --addr)
                return 0
                ;;
        esac
        if [[ $cur == -* ]]; then
            COMPREPLY=($(compgen -W "$global_opts" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$cmds" -- "$cur"))
        fi
        return 0
    fi

    # Count positional arguments after the subcommand (excluding flags & their values)
    local pos=0
    local j=$((cmd_idx + 1))
    while (( j < COMP_CWORD )); do
        w=${COMP_WORDS[j]}
        case "$w" in
            -o|--output|-d|--dest|-p|--page|-t|--target|-f|--folder|--sort|--mark|--revision|--format|--pin-file|--name|--password)
                (( j += 2 ))
                continue
                ;;
            -*)
                (( j += 1 ))
                continue
                ;;
            *)
                (( pos += 1 ))
                (( j += 1 ))
                ;;
        esac
    done

    # Complete per-subcommand flags and arguments
    case "$cmd" in
        ls)
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-E --regex -l --long -R --recursive -h --help" -- "$cur"))
            elif (( pos == 0 )); then
                _quaderno_device_paths all "$cur"
            fi
            ;;
        get)
            case "$prev" in
                -o|--output) _quaderno_local_files "$cur" file; return 0 ;;
            esac
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-E --regex -r --recursive -o --output -h --help" -- "$cur"))
            else
                _quaderno_device_paths all "$cur"
            fi
            ;;
        put)
            case "$prev" in
                -d|--dest) _quaderno_device_paths dirs "$cur"; return 0 ;;
            esac
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-r --recursive -d --dest -h --help" -- "$cur"))
            else
                _quaderno_local_files "$cur" file
            fi
            ;;
        mkdir)
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-p --parents -h --help" -- "$cur"))
            else
                _quaderno_device_paths dirs "$cur"
            fi
            ;;
        mv|cp)
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-E --regex -h --help" -- "$cur"))
            else
                _quaderno_device_paths all "$cur"
            fi
            ;;
        rm)
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-E --regex -r --recursive -y --yes -h --help" -- "$cur"))
            else
                _quaderno_device_paths all "$cur"
            fi
            ;;
        sync)
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "--delete -n --dry-run --force -h --help" -- "$cur"))
            elif (( pos == 0 )); then
                _quaderno_local_files "$cur" dir
            elif (( pos == 1 )); then
                _quaderno_device_paths dirs "$cur"
            fi
            ;;
        open)
            case "$prev" in
                -p|--page) return 0 ;;
            esac
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-E --regex -p --page -h --help" -- "$cur"))
            elif (( pos == 0 )); then
                _quaderno_device_paths all "$cur"
            fi
            ;;
        search)
            case "$prev" in
                -t|--target) COMPREPLY=($(compgen -W "meta body mark" -- "$cur")); return 0 ;;
                -f|--folder) _quaderno_device_paths dirs "$cur"; return 0 ;;
                --sort)      COMPREPLY=($(compgen -W "name title author date read size created" -- "$cur")); return 0 ;;
                --mark)      COMPREPLY=($(compgen -W "all hand highlight" -- "$cur")); return 0 ;;
            esac
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-t --target -f --folder --notes --docs --sort --desc --mark -l --long -h --help" -- "$cur"))
            fi
            ;;
        replace)
            case "$prev" in
                --revision) return 0 ;;
            esac
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "--revision -h --help" -- "$cur"))
            elif (( pos == 0 )); then
                _quaderno_local_files "$cur" pdf
            elif (( pos == 1 )); then
                _quaderno_device_paths all "$cur"
            fi
            ;;
        annotations)
            case "$prev" in
                -o|--output) _quaderno_local_files "$cur" file; return 0 ;;
            esac
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-o --output -h --help" -- "$cur"))
            elif (( pos == 0 )); then
                _quaderno_device_paths all "$cur"
            fi
            ;;
        templates)
            if (( pos == 0 )); then
                COMPREPLY=($(compgen -W "ls add rename rm" -- "$cur"))
            elif [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "--name -h --help" -- "$cur"))
            else
                _quaderno_local_files "$cur" pdf
            fi
            ;;
        wifi)
            if (( pos == 0 )); then
                COMPREPLY=($(compgen -W "status on off scan ls add rm" -- "$cur"))
            elif [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "--security --password --password-stdin -h --help" -- "$cur"))
            fi
            ;;
        power)
            (( pos == 0 )) && COMPREPLY=($(compgen -W "sleep wake reboot shutdown" -- "$cur"))
            ;;
        firmware)
            if (( pos == 0 )); then
                COMPREPLY=($(compgen -W "precheck upload update" -- "$cur"))
            elif [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "--yes -h --help" -- "$cur"))
            else
                _quaderno_local_files "$cur" file
            fi
            ;;
        pen-buttons)
            if (( pos == 0 )); then
                COMPREPLY=($(compgen -W "ls set" -- "$cur"))
            elif (( pos == 1 )); then
                COMPREPLY=($(compgen -W "side tail" -- "$cur"))
            elif (( pos == 2 )); then
                COMPREPLY=($(compgen -W "highlight eraser selection marker secondary_color none expansion" -- "$cur"))
            fi
            ;;
        certs)
            if (( pos == 0 )); then
                COMPREPLY=($(compgen -W "ls add-ca add-client clear" -- "$cur"))
            elif [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "--name --password --password-stdin --yes -h --help" -- "$cur"))
            else
                _quaderno_local_files "$cur" file
            fi
            ;;
        pastlog)
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "-h --help" -- "$cur"))
            elif (( pos == 0 )); then
                _quaderno_local_files "$cur" file
            fi
            ;;
        config)
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "--all -h --help" -- "$cur"))
            elif (( pos == 0 )); then
                COMPREPLY=($(compgen -W "owner timezone date_format time_format timeout_to_standby mtp regulation_voltage use_mode annotations_permission additional_client_registration control_servers_editable initialized_flag wifi wifi_apmode wifi_sleep_policy pen_buttons_function wifi_accesspoints" -- "$cur"))
            fi
            ;;
        info)
            [[ $cur == -* ]] && COMPREPLY=($(compgen -W "--json -h --help" -- "$cur"))
            ;;
        screenshot)
            case "$prev" in
                --format) COMPREPLY=($(compgen -W "png jpeg" -- "$cur")); return 0 ;;
            esac
            if [[ $cur == -* ]]; then
                COMPREPLY=($(compgen -W "--format -h --help" -- "$cur"))
            elif (( pos == 0 )); then
                _quaderno_local_files "$cur" file
            fi
            ;;
        pair)
            case "$prev" in
                --pin-file) _quaderno_local_files "$cur" file; return 0 ;;
            esac
            [[ $cur == -* ]] && COMPREPLY=($(compgen -W "--force --pin-file -h --help" -- "$cur"))
            ;;
        viewing|marks|ping|time|addr|linux-setup)
            [[ $cur == -* ]] && COMPREPLY=($(compgen -W "-h --help" -- "$cur"))
            ;;
    esac
}

complete -F _quaderno quaderno
