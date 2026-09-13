{
  description = "Homelab Core — Turnkey Declarative NixOS Appliance for roadtotech.me";

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
        python3
        python3Packages.pytest
        python3Packages.pyyaml
        ruff
      ];
    };

    checks.${system} = {
      test-suite = pkgs.runCommand "core-tests" {
        nativeBuildInputs = with pkgs; [
          python3
          python3Packages.pytest
          python3Packages.pyyaml
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
