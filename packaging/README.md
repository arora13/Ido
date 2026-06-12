# Packaging

Build the website first, then create the companion executable:

```bash
cd web && npm ci && npm run build
cd ..
python -m pip install -e '.[packaging]'
pyinstaller packaging/ido.spec --clean
```

The release workflow wraps the executable as:

- macOS: `.dmg`
- Windows: Inno Setup `.exe`
- Linux: `.tar.gz`, `.deb`, and AppImage when the host tools are available

Signing credentials are optional repository secrets. Unsigned artifacts remain
available for development releases.
