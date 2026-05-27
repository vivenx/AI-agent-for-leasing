{ pkgs }:
{
  deps = [
    pkgs.python311
    pkgs.chromium
    pkgs.chromedriver

    pkgs.glib
    pkgs.nss
    pkgs.fontconfig

    pkgs.xorg.libX11
    pkgs.xorg.libXcomposite
    pkgs.xorg.libXcursor
    pkgs.xorg.libXdamage
    pkgs.xorg.libXext
    pkgs.xorg.libXfixes
    pkgs.xorg.libXi
    pkgs.xorg.libXrandr
    pkgs.xorg.libXrender
    pkgs.xorg.libXtst
    pkgs.xorg.libxcb
    pkgs.cups
  ];
}
