# 🐚 Server Shell Configuration
# Declarative bash environment tailored for the headless homelab server.
# This replaces the laptop-inherited ~/.bashrc with a clean, server-appropriate profile.
# Delivered via /etc/bashrc — no Home Manager required.

{ pkgs, ... }:

{
  # ── Shell-integrated programs ─────────────────────────────────────────────────
  # These handle both installation and bash hook injection automatically
  programs.zoxide.enable = true;   # smart cd with bash init wired into /etc/bashrc
  programs.starship.enable = true; # cross-shell prompt
  programs.direnv.enable = true;   # per-project env vars via .envrc files

  # ── Additional shell packages ─────────────────────────────────────────────────
  environment.systemPackages = with pkgs; [
    fastfetch  # system info dashboard on shell entry
    fzf        # fuzzy finder (keybindings handled in interactiveShellInit)
    bat        # syntax-highlighted cat
    qpdf       # PDF decryption (used by pdf_dc function)
  ];

  # ── Environment & PATH ────────────────────────────────────────────────────────
  # Export Core scripts and user bin to /etc/set-environment so all subshells inherit them
  environment.extraInit = ''
    export PATH="$HOME/Core/scripts:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  '';

  # ── Bash configuration ────────────────────────────────────────────────────────
  programs.bash = {
    shellAliases = {
      # Traversal
      ".."    = "cd ..";
      "..."   = "cd ../..";
      "...."  = "cd ../../..";
      "....." = "cd ../../../..";

      # eza-based directory listing
      ls  = "eza --group-directories-first --header --icons";
      la  = "eza -a";
      ll  = "eza -l";
      lla = "eza -la";
      lt  = "eza --tree";
      # zoxide interactive jump with eza preview
      zi  = "zoxide query -i --preview 'eza --tree --level 2 --color=always {}'";

      # Git shortcuts
      ga     = "git add";
      gc     = "git commit -m";
      gp     = "git push";
      gpl    = "git pull";
      gs     = "git status";
      gst    = "git stash";
      gsp    = "git stash && git pull";
      gfo    = "git fetch origin";
      gcheck = "git checkout";
      gadc   = "git add -A && git diff --staged";

      # Editor & system
      v          = "nvim";
      ff         = "fastfetch --logo none";
      reload     = "exec bash";
      pgoog      = "ping google.com -c 3";
      wifi       = "nmtui";
      # NixOS rebuild targeting this flake
      nix-switch = "sudo nixos-rebuild switch --flake ~/Core#server";
    };

    interactiveShellInit = ''
      # ── User PATH ────────────────────────────────────────────────────────────
      export PATH="$HOME/Core/scripts:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

      # ── History ───────────────────────────────────────────────────────────────
      HISTFILESIZE=100000
      HISTSIZE=10000
      shopt -s histappend
      shopt -s extglob
      shopt -s globstar
      shopt -s checkjobs

      # ── Readline keybindings ─────────────────────────────────────────────────
      if [[ -n "$BASH_VERSION" ]]; then
        bind '"\C-w": "\eb\ed"'    2>/dev/null  # Ctrl+W: delete word backward
        bind '"\e[3;5~": kill-word' 2>/dev/null # Ctrl+Delete: delete word forward
        bind '"\e\x7f": backward-kill-word' 2>/dev/null
        bind '"\e\b":   backward-kill-word' 2>/dev/null
      fi

      # ── fzf shell integration ─────────────────────────────────────────────────
      if command -v fzf &>/dev/null; then
        eval "$(fzf --bash)"
      fi

      # ── Shell entry dashboard ─────────────────────────────────────────────────
      fastfetch --logo none

      # ── Git workflow functions ────────────────────────────────────────────────

      # gacp: Stage all, commit with message, and push to current branch
      gacp() {
        if [ -z "$1" ]; then
          echo "Usage: gacp <commit-message>"
          return 1
        fi
        git add -A
        git commit -m "$1"
        local branch_name
        branch_name=$(git branch --show-current)
        git push origin "$branch_name"
        echo "Pushed to origin/$branch_name"
      }

      # gitignore: Append pattern(s) to .gitignore and auto-commit
      gitignore() {
        if [ $# -eq 0 ]; then
          echo "Usage: gitignore <pattern> [pattern...]"
          return 1
        fi
        local GIT_ROOT
        GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
        if [ -z "$GIT_ROOT" ]; then
          echo "Error: Not a Git repository."
          return 1
        fi
        local GIT_IGNORE_FILE="$GIT_ROOT/.gitignore"
        [ -f "$GIT_IGNORE_FILE" ] || touch "$GIT_IGNORE_FILE"
        if [ -s "$GIT_IGNORE_FILE" ] && [ "$(tail -c1 "$GIT_IGNORE_FILE" | wc -l)" -eq 0 ]; then
          echo "" >> "$GIT_IGNORE_FILE"
        fi
        local added_count=0 commit_msg_list=""
        for pattern in "$@"; do
          if rg -Fxq "$pattern" "$GIT_IGNORE_FILE" 2>/dev/null; then
            echo "Skipping '$pattern' (already in .gitignore)"
          else
            echo "$pattern" >> "$GIT_IGNORE_FILE"
            echo "Added '$pattern'"
            (( added_count++ ))
            commit_msg_list+="$pattern, "
          fi
        done
        if [ "$added_count" -gt 0 ]; then
          commit_msg_list="''${commit_msg_list%, }"
          git add "$GIT_IGNORE_FILE"
          git commit -m "Add to .gitignore: $commit_msg_list"
          git push
        else
          echo "No new patterns were added."
        fi
      }

      # ── PDF utilities ─────────────────────────────────────────────────────────

      # pdf_dc: Decrypt a password-protected PDF
      # Reads password from /run/secrets/pdf_decrypt_password if not supplied
      pdf_dc() {
        local secret_file="/run/secrets/pdf_decrypt_password"
        local input_file="$1"
        local password="$2"
        if [[ -z "$password" ]] && [[ -f "$secret_file" ]]; then
          password=$(cat "$secret_file")
        fi
        if [[ -z "$password" ]]; then
          echo "Error: No password provided and default secret not found" >&2
          return 1
        fi
        if [[ ! -f "$input_file" ]]; then
          echo "Error: Input file '$input_file' not found" >&2
          return 1
        fi
        local output_file="''${input_file%.pdf}_decrypted.pdf"
        if [[ -f "$output_file" ]]; then
          read -rp "Output '$output_file' exists. Overwrite? (y/n): " overwrite
          [[ "$overwrite" != "y" ]] && { echo "Operation cancelled."; return 1; }
        fi
        echo "Decrypting $input_file..."
        if qpdf --password="$password" --decrypt "$input_file" "$output_file"; then
          echo "✅ Decryption successful: $output_file"
        else
          local exit_code=$?
          echo "❌ Decryption failed (qpdf exit code: $exit_code)"
          return "$exit_code"
        fi
      }

      # ── Fuzzy directory navigation ────────────────────────────────────────────

      # Internal: fuzzy-select a subdirectory with eza preview
      _fuzzy_select_dir() {
        local base="$1" query="$2" max_depth="''${3:-2}"
        [[ -d "$base" ]] || return 1
        (echo "$base"; fd . "$base" --type d --mindepth 1 --max-depth "$max_depth" 2>/dev/null) |
          fzf --query="$query" --select-1 --exit-0 \
              --preview 'eza --tree --level 2 --icons=always --color=always {}' \
              --preview-window="right:50%:rounded"
      }

      # Internal: cd into a directory
      _jump_to() {
        local dir="$1"
        [[ -d "$dir" ]] || return 1
        cd "$dir" || return 1
      }

      # jump: Fuzzy-select and enter a subdirectory under a base path
      jump() {
        local dir
        dir=$(_fuzzy_select_dir "$1" "$2" "''${3:-2}") || return
        _jump_to "$dir"
      }

      # Shorthand jump targets
      prj()  { jump "$HOME/Projects"    "$1"; }  # workspace projects
      dep()  { jump "$HOME/Deployments" "$1"; }  # deployment directories
      med()  { jump "/media"            "$1"; }  # mounted media storage
      core() { jump "$HOME/Core"        "$1"; }  # homelab-core subdirectories
    '';
  };
}
