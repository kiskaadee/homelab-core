{
  description = "Homelab Core — Turnkey Declarative NixOS Appliance";

  inputs = {
    nixpkgs.url = "https://channels.nixos.org/nixos-unstable/nixexprs.tar.zst";

    sops-nix = {
      url = "github:Mic92/sops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, sops-nix, ... }@inputs:
  let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};
    pythonEnv = pkgs.python3.withPackages (ps: with ps; [
      pyyaml
      types-pyyaml
      pytest
      pydantic
    ]);
  in
  {
    nixosConfigurations.server = nixpkgs.lib.nixosSystem {
      inherit system;
      specialArgs = { inherit inputs; };
      modules = [
        ./nixos/configuration.nix
        sops-nix.nixosModules.sops
      ];
    };

    devShells.${system}.default = pkgs.mkShell {
      buildInputs = with pkgs; [
        pythonEnv
        pyright
        ruff
      ];
      shellHook = ''
        git config core.hooksPath .githooks 2>/dev/null || true
        if [ -d .venv ] && [ ! -L .venv ]; then
          rm -rf .venv
        fi
        ln -sfn ${pythonEnv} .venv
      '';
    };

    checks.${system} = {
      test-suite = pkgs.runCommand "core-tests" {
        nativeBuildInputs = with pkgs; [
          git
          pythonEnv
          ruff
        ];
      } ''
        mkdir -p $out
        cp -r ${self} source
        chmod -R u+w source
        cd source
        HOME=/tmp ruff check --no-cache .
        HOME=/tmp pytest -o cache_dir=/tmp/.pytest_cache tests/
      '';
    };
  };
}
