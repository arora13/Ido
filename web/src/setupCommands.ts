export type Platform = 'macos' | 'windows' | 'linux'

const REPOSITORY = 'https://github.com/arora13/Ido'

export function detectPlatform(): Platform {
  if (typeof navigator === 'undefined') return 'macos'
  const ua = navigator.userAgent.toLowerCase()
  if (ua.includes('win')) return 'windows'
  if (ua.includes('mac')) return 'macos'
  return 'linux'
}

export function buildOpenScadSetupCommands(platform: Platform): string {
  const openScadInstall = {
    macos: 'brew install --cask openscad',
    windows: 'winget install OpenSCAD.OpenSCAD',
    linux: 'sudo apt-get install -y openscad  # package name varies by distro',
  }[platform]

  const venvActivate = {
    macos: 'source .venv/bin/activate',
    windows: '.venv\\Scripts\\activate',
    linux: 'source .venv/bin/activate',
  }[platform]

  return [
    '# 1. Install OpenSCAD',
    openScadInstall,
    '',
    '# 2. Install idō',
    `git clone ${REPOSITORY}.git`,
    'cd Ido',
    'python3 -m venv .venv',
    venvActivate,
    'pip install -e .',
    'cp .env.example .env',
    '# Edit .env and set OPENAI_API_KEY=sk-...  (or use demo mode below)',
    '',
    '# 3. Open OpenSCAD with idō',
    'ido open openscad',
    '',
    '# No API key yet? Uncomment and run:',
    '# CAD_AGENT_DEMO_MODE=true ido open openscad',
  ].join('\n')
}

export function buildPromptCommand(
  tool: 'blender' | 'openscad',
  prompt: string,
): string {
  const escaped = prompt.replace(/\\/g, '\\\\').replace(/"/g, '\\"')
  return `ido prompt --tool ${tool} "${escaped}"`
}
