import { FormEvent, useEffect, useRef, useState } from 'react'
import {
  buildOpenScadSetupCommands,
  buildPromptCommand,
  detectPlatform,
  type Platform,
} from './setupCommands'

const REPOSITORY = 'https://github.com/arora13/Ido'
const RELEASES = `${REPOSITORY}/releases/latest/download`
// Served by the FastAPI backend on ido serve and Render (see /downloads/ido_blender.zip).
const BLENDER_ADDON = '/downloads/ido_blender.zip'

type Tool = 'blender' | 'openscad'
type RuntimeStatus = {
  tool: Tool | 'companion'
  phase: string
  message: string
  artifacts: Record<string, string>
  recent_errors: string[]
  provider?: string | null
  inference_provider?: string | null
  clickhouse_enabled?: boolean
  clickhouse_exported?: boolean | null
  request_id?: string | null
}

type IntegrationsStatus = {
  provider: string
  pioneer_configured: boolean
  pioneer_model?: string | null
  clickhouse_enabled: boolean
  clickhouse_reachable?: boolean | null
  clickhouse_table?: string | null
  guild_enabled?: boolean
  openui_active?: boolean
  composio_enabled?: boolean
  airbyte_enabled?: boolean
  truefoundry_available?: boolean
  render_blueprint?: boolean
  capabilities?: string[]
}

type OpenUIElement = {
  type: string
  props?: {
    text?: string
    title?: string
    emphasis?: boolean
    body?: OpenUIElement[]
  }
}

type SceneObject = { id: string; label: string; type?: string; shape?: string }

type PromptResult = {
  status?: string
  error?: string
  request_id?: string
  provider?: string
  scene_headline?: string
  openui_elements?: OpenUIElement[]
  clickhouse_exported?: boolean
  airbyte_context_exported?: boolean
  composio_status?: string | null
  guild_trace_url?: string | null
  ir?: { scene?: { objects?: SceneObject[] }; history?: string[] }
  trace?: Array<{ step?: string; status?: string; metadata?: { inference_provider?: string } }>
}

type PromptStreamEvent =
  | { type: 'status'; message?: string; elapsed_seconds?: number }
  | {
      type: 'object'
      id?: string
      name?: string
      object_type?: string
      shape?: string
      status?: string
    }
  | { type: 'code'; content?: string }
  | { type: 'code_reset' }
  | { type: 'done'; result?: PromptResult }
  | { type: 'error'; message?: string; result?: PromptResult }

async function readPromptStream(
  response: Response,
  onEvent: (event: PromptStreamEvent) => void,
) {
  if (!response.ok) {
    throw new Error(`Generation request failed with HTTP ${response.status}`)
  }
  if (!response.body) {
    throw new Error('Generation response did not include a stream')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finished = false

  const consumeFrames = (final = false) => {
    buffer += final ? decoder.decode() : ''
    const normalized = buffer.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    const frames = normalized.split('\n\n')
    if (final) {
      buffer = ''
    } else {
      buffer = frames.pop() ?? ''
    }
    for (const frame of frames) {
      const data = frame
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())
        .join('\n')
      if (!data) continue
      const event = JSON.parse(data) as PromptStreamEvent
      onEvent(event)
      if (event.type === 'done' || event.type === 'error') finished = true
    }
  }

  while (!finished) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    consumeFrames()
  }
  if (!finished) {
    consumeFrames(true)
  }
  if (finished) {
    await reader.cancel()
  }
  if (!finished) {
    throw new Error('Generation stream ended before a final result')
  }
}

const downloadLinks = [
  ['macOS', `${RELEASES}/ido-macos.dmg`, 'Apple Silicon + Intel'],
  ['Windows', `${RELEASES}/ido-windows.exe`, 'Windows 10 and 11'],
  ['Linux', `${RELEASES}/ido-linux.AppImage`, 'AppImage'],
] as const

const PLATFORM_LABELS: Record<Platform, string> = {
  macos: 'macOS',
  windows: 'Windows',
  linux: 'Linux',
}

function OpenUIPreview({ elements }: { elements: OpenUIElement[] }) {
  if (!elements.length) return null
  const renderElement = (element: OpenUIElement, index: number) => {
    const text = element.props?.text ?? ''
    if (element.type === 'Heading') {
      return (
        <strong key={index} className="openui-heading">
          {text}
        </strong>
      )
    }
    if (element.type === 'Card') {
      return (
        <div key={index} className="openui-card">
          {element.props?.title ? <span>{element.props.title}</span> : null}
          {element.props?.body?.map((child, childIndex) =>
            renderElement(child, childIndex),
          )}
        </div>
      )
    }
    return (
      <p
        key={index}
        className={element.props?.emphasis ? 'openui-emphasis' : undefined}
      >
        {text}
      </p>
    )
  }
  return (
    <div className="openui-preview" aria-label="OpenUI preview">
      {elements.map((element, index) => renderElement(element, index))}
    </div>
  )
}

function sponsorBadgeLabel(name: string, integrations: IntegrationsStatus | null): string {
  if (!integrations) return '…'
  switch (name) {
    case 'OpenUI':
      return integrations.openui_active ? 'active' : 'off'
    case 'Guild':
      return integrations.guild_enabled ? 'on' : 'off'
    case 'ClickHouse':
      if (!integrations.clickhouse_enabled) return 'off'
      return integrations.clickhouse_reachable ? 'connected' : 'unreachable'
    case 'Composio':
      return integrations.composio_enabled ? 'on' : 'off'
    case 'Airbyte':
      return integrations.airbyte_enabled ? 'on' : 'off'
    case 'Pioneer':
      return integrations.pioneer_configured ? integrations.provider : 'not configured'
    case 'Render':
      return integrations.render_blueprint ? 'blueprint' : '—'
    case 'TrueFoundry':
      return integrations.truefoundry_available ? 'ready' : '—'
    default:
      return '—'
  }
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5 12h13M13 6l6 6-6 6" />
    </svg>
  )
}

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="8" y="8" width="11" height="11" />
      <path d="M16 8V5H5v11h3" />
    </svg>
  )
}

function ToolPreview({ tool }: { tool: Tool }) {
  return (
    <div className={`tool-preview ${tool}`}>
      <div className="preview-toolbar">
        <span>{tool === 'blender' ? 'Layout  Modeling  Sculpting  Shading' : 'ido_current.scad'}</span>
        <span>idō</span>
      </div>
      <div className="preview-body">
        <div className="scene-grid" aria-hidden="true">
          <div className="cad-object">
            <span className="cad-base" />
            <span className="cad-top" />
            <span className="cad-hole" />
          </div>
        </div>
        <div className="preview-sidebar">
          <strong>{tool === 'blender' ? 'idō prompt' : 'OpenSCAD'}</strong>
          <p>{tool === 'blender' ? 'Make a compact enclosure with a cable port.' : 'difference() {'}</p>
          <div className="code-lines">
            <i />
            <i />
            <i />
            <i />
          </div>
          <span className="preview-action">{tool === 'blender' ? 'Generate' : 'Render complete'}</span>
        </div>
      </div>
    </div>
  )
}

function CopyCommand({ command, multiline = false }: { command: string; multiline?: boolean }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(command)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1400)
  }
  return (
    <div className={`command${multiline ? ' command-multiline' : ''}`}>
      <code>{command}</code>
      <button type="button" onClick={copy} aria-label="Copy command">
        <CopyIcon />
        <span>{copied ? 'Copied' : 'Copy'}</span>
      </button>
    </div>
  )
}

function SetupCommandGenerator() {
  const [platform, setPlatform] = useState<Platform>(() => detectPlatform())
  const [visible, setVisible] = useState(true)
  const commands = buildOpenScadSetupCommands(platform)

  return (
    <div className="setup-command-generator">
      <div className="setup-command-actions">
        <button type="button" className="outline-button" onClick={() => setVisible(true)}>
          Generate setup commands
        </button>
        <div className="platform-picker" role="group" aria-label="Target platform">
          {(Object.keys(PLATFORM_LABELS) as Platform[]).map((value) => (
            <button
              key={value}
              type="button"
              className={platform === value ? 'active' : undefined}
              onClick={() => {
                setPlatform(value)
                setVisible(true)
              }}
            >
              {PLATFORM_LABELS[value]}
            </button>
          ))}
        </div>
      </div>
      {visible ? (
        <CopyCommand command={commands} multiline />
      ) : null}
    </div>
  )
}

function SetupSection({
  id,
  tool,
  title,
  description,
  steps,
  command,
  setupGenerator,
}: {
  id: string
  tool: Tool
  title: string
  description: string
  steps: string[]
  command: string
  setupGenerator?: boolean
}) {
  return (
    <section className="setup-section rule-section" id={id}>
      <div className="section-copy">
        <h2>{title}</h2>
        <p>{description}</p>
        <ol className="steps">
          {steps.map((step, index) => (
            <li key={step}>
              <span>{index + 1}</span>
              <p>{step}</p>
            </li>
          ))}
        </ol>
        {setupGenerator ? <SetupCommandGenerator /> : null}
        {tool === 'blender' ? (
          <a className="outline-button" href={BLENDER_ADDON}>
            Download ido_blender.zip <ArrowIcon />
          </a>
        ) : null}
      </div>
      <div className="section-media">
        <ToolPreview tool={tool} />
        <div className="launch-row">
          <div>
            <small>Open {tool === 'blender' ? 'Blender' : 'OpenSCAD'} with idō</small>
            <CopyCommand command={command} />
          </div>
          <div className="tool-ready">
            <span>{tool === 'blender' ? 'Blender ready' : 'OpenSCAD ready'}</span>
          </div>
        </div>
      </div>
    </section>
  )
}

function LocalControl() {
  const [tool, setTool] = useState<Tool>('blender')
  const [prompt, setPrompt] = useState('')
  const [status, setStatus] = useState<RuntimeStatus | null>(null)
  const [integrations, setIntegrations] = useState<IntegrationsStatus | null>(null)
  const [lastResult, setLastResult] = useState<PromptResult | null>(null)
  const [lastPrompt, setLastPrompt] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [generationStatus, setGenerationStatus] = useState('')
  const [generatedObjects, setGeneratedObjects] = useState<SceneObject[]>([])
  const [streamedCode, setStreamedCode] = useState('')
  const local = ['localhost', '127.0.0.1'].includes(window.location.hostname)

  useEffect(() => {
    if (!local) return
    let active = true
    const refresh = async () => {
      try {
        const [statusResponse, integrationsResponse] = await Promise.all([
          fetch('/api/status'),
          fetch('/api/integrations'),
        ])
        if (active && statusResponse.ok) setStatus(await statusResponse.json())
        if (active && integrationsResponse.ok) {
          setIntegrations(await integrationsResponse.json())
        }
      } catch {
        // The static docs site deliberately has no local API.
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 1000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [local])

  if (!local) return null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!prompt.trim()) return
    setSubmitting(true)
    setGenerationStatus(tool === 'blender' ? 'Parsing prompt...' : 'Generating...')
    setGeneratedObjects([])
    setStreamedCode('')
    const submittedPrompt = prompt.trim()
    const path = tool === 'openscad' ? '/api/openscad/prompt' : '/api/prompt/stream'
    const currentIr =
      lastResult?.status === 'ok' && lastResult.ir ? lastResult.ir : null
    const payload =
      tool === 'openscad'
        ? { prompt: submittedPrompt, current_ir: currentIr }
        : { prompt: submittedPrompt, current_ir: currentIr, target_tool: 'blender' }
    try {
      let response = await fetch(path, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (tool === 'blender' && [404, 405].includes(response.status)) {
        setGenerationStatus('Streaming unavailable; waiting for generation...')
        response = await fetch('/api/prompt', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(payload),
        })
        const body = await response.json()
        setLastResult(body)
        setGenerationStatus('')
        setLastPrompt(submittedPrompt)
        setPrompt('')
        return
      }
      if (tool === 'blender') {
        await readPromptStream(response, (streamEvent) => {
          if (streamEvent.type === 'status') {
            const elapsed = streamEvent.elapsed_seconds
            setGenerationStatus(
              `${streamEvent.message ?? 'Generating scene...'}${
                elapsed && elapsed > 0 ? ` ${elapsed}s` : ''
              }`,
            )
          } else if (streamEvent.type === 'object') {
            const item = {
              id: streamEvent.id ?? streamEvent.name ?? 'object',
              label: streamEvent.name ?? streamEvent.id ?? 'object',
              type: streamEvent.object_type,
            }
            setGeneratedObjects((current) =>
              current.some((existing) => existing.id === item.id)
                ? current
                : [...current, item],
            )
          } else if (streamEvent.type === 'code') {
            setGenerationStatus('Streaming generated scene...')
            setStreamedCode((current) =>
              `${current}${streamEvent.content ?? ''}`.slice(-8000),
            )
          } else if (streamEvent.type === 'code_reset') {
            setStreamedCode('')
            setGeneratedObjects([])
          } else if (streamEvent.type === 'done' && streamEvent.result) {
            setLastResult(streamEvent.result)
            setGenerationStatus('Blender scene is ready')
          } else if (streamEvent.type === 'error') {
            setLastResult(
              streamEvent.result ?? {
                status: 'error',
                error: streamEvent.message ?? 'Generation failed',
              },
            )
            setGenerationStatus(streamEvent.message ?? 'Generation failed')
          }
        })
      } else {
        const body = await response.json()
        setLastResult(body)
        setGenerationStatus('')
      }
      setLastPrompt(submittedPrompt)
      setPrompt('')
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Generation failed'
      setLastResult({ status: 'error', error: message })
      setGenerationStatus(message)
    } finally {
      setSubmitting(false)
    }
  }

  const objectLabels = lastResult?.ir?.scene?.objects?.map((item) => item.label) ?? []

  const inferenceProvider =
    status?.inference_provider ??
    lastResult?.trace?.find(
      (event) => event.step === 'parse' && event.status === 'completed',
    )?.metadata?.inference_provider

  return (
    <section className="local-control rule-section" aria-label="Local companion controls">
      <div>
        <h2>Local companion</h2>
        <p>{status?.message ?? 'Connected to idō on this machine.'}</p>
        <div className="integration-badges">
          {(['OpenUI', 'Guild', 'ClickHouse', 'Composio', 'Airbyte', 'Pioneer', 'Render', 'TrueFoundry'] as const).map(
            (name) => (
              <span className="integration-badge" key={name}>
                {name}: {sponsorBadgeLabel(name, integrations)}
              </span>
            ),
          )}
        </div>
        {integrations?.capabilities?.length ? (
          <ul className="sponsor-capabilities">
            {integrations.capabilities.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        ) : null}
        {(status?.provider || inferenceProvider || lastResult?.clickhouse_exported) && (
          <div className="integration-meta">
            {status?.provider && <span>Provider: {status.provider}</span>}
            {inferenceProvider && <span>Inference: {inferenceProvider}</span>}
            {lastResult?.clickhouse_exported && (
              <span>ClickHouse: trace exported ({lastResult.request_id?.slice(0, 12)}…)</span>
            )}
          </div>
        )}
      </div>
      <form onSubmit={submit}>
        <select value={tool} onChange={(event) => setTool(event.target.value as Tool)}>
          <option value="blender">Blender</option>
          <option value="openscad">OpenSCAD</option>
        </select>
        <input
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="e.g. make a cozy bedroom, make a chair, make a desk"
        />
        <button disabled={submitting}>{submitting ? 'Working' : 'Prompt'}</button>
      </form>
      {(submitting || generatedObjects.length > 0) && (
        <div className="generation-progress" aria-live="polite">
          <strong>{generationStatus || 'Generating scene...'}</strong>
          {generatedObjects.length > 0 && (
            <ol>
              {generatedObjects.map((item) => (
                <li key={item.id}>
                  <code>{item.type ?? 'object'}</code> {item.label}
                </li>
              ))}
            </ol>
          )}
          {streamedCode && <pre>{streamedCode}</pre>}
        </div>
      )}
      {lastResult && (
        <div className="prompt-result">
          <strong>
            {lastResult.status === 'ok'
              ? lastResult.scene_headline ?? `Built ${objectLabels.length} objects`
              : `Error: ${lastResult.error ?? lastResult.status}`}
          </strong>
          {lastResult.status === 'ok' && objectLabels.length > 0 && (
            <p>{objectLabels.slice(0, 12).join(', ')}{objectLabels.length > 12 ? '…' : ''}</p>
          )}
          {lastResult.status === 'ok' && lastResult.openui_elements?.length ? (
            <OpenUIPreview elements={lastResult.openui_elements} />
          ) : null}
          {lastPrompt ? (
            <CopyCommand command={buildPromptCommand(tool, lastPrompt)} />
          ) : null}
        </div>
      )}
    </section>
  )
}

export default function App() {
  const introRef = useRef<HTMLElement>(null)

  useEffect(() => {
    const intro = introRef.current
    if (!intro || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return

    let frame = 0
    const updateHero = () => {
      frame = 0
      const progress = Math.min(Math.max(window.scrollY / (window.innerHeight * 0.85), 0), 1)
      intro.style.setProperty('--hero-progress', progress.toString())
    }
    const scheduleUpdate = () => {
      if (!frame) frame = window.requestAnimationFrame(updateHero)
    }

    updateHero()
    window.addEventListener('scroll', scheduleUpdate, { passive: true })
    window.addEventListener('resize', scheduleUpdate)
    return () => {
      window.removeEventListener('scroll', scheduleUpdate)
      window.removeEventListener('resize', scheduleUpdate)
      if (frame) window.cancelAnimationFrame(frame)
    }
  }, [])

  return (
    <>
      <header className="site-header">
        <a className="brand" href="#top" aria-label="idō home">
          idō
        </a>
        <nav aria-label="Primary navigation">
          <a href="#overview">Overview</a>
          <a href="#pipeline">Pipeline</a>
          <a href="#tools">Tools</a>
          <a href="#blender">Setup</a>
        </nav>
        <a className="header-cta" href="#download">
          Get idō
        </a>
      </header>

      <main id="top">
        <section className="intro-hero" ref={introRef}>
          <div className="perspective-grid" aria-hidden="true">
            <span className="grid-plane grid-ceiling" />
            <span className="grid-plane grid-floor" />
          </div>
          <div className="intro-content">
            <p className="intro-kicker">Universal agent harness for 3D design</p>
            <h1 aria-label="idō">i d ō</h1>
            <p className="intro-tagline">
              Say what you want to build. <em>Watch it appear</em> as native,
              editable objects in the tools you already use.
            </p>
            <p className="intro-description">
              idō turns natural language into validated Engineering IR and builds it
              live in Blender and OpenSCAD. Local-first: your prompts, files, and
              geometry stay on your machine.
            </p>
            <div className="intro-actions">
              <a className="solid-button" href="#download">
                Get idō <ArrowIcon />
              </a>
              <a className="outline-button" href="#blender">
                Open setup
              </a>
              <a className="outline-button" href={REPOSITORY}>
                GitHub
              </a>
            </div>
          </div>
          <a className="scroll-cue" href="#overview">Scroll to initialize</a>
        </section>

        <section className="overview-section rule-section" id="overview">
          <div>
            <small>OVERVIEW</small>
            <h2>One prompt.<br /><em>Real geometry.</em></h2>
            <p>
              idō is an AI agent that speaks CAD. A natural-language request becomes
              a structured, validated scene built object by object in your viewport.
            </p>
          </div>
          <ol className="feature-list">
            <li>
              <span>01</span>
              <div>
                <strong>Native, editable objects</strong>
                <p>Every object lands as a real primitive with explicit dimensions.</p>
              </div>
            </li>
            <li>
              <span>02</span>
              <div>
                <strong>Code you can touch</strong>
                <p>Inspect and revise the generated scene without giving up control.</p>
              </div>
            </li>
            <li>
              <span>03</span>
              <div>
                <strong>Iterative by design</strong>
                <p>Follow-up prompts update the current design instead of starting over.</p>
              </div>
            </li>
          </ol>
        </section>

        <section className="pipeline-section rule-section" id="pipeline">
          <div className="pipeline-heading">
            <small>PIPELINE</small>
            <h2>Every request flows through <em>four stages.</em></h2>
            <p>Nothing reaches a CAD tool until the generated Engineering IR validates.</p>
          </div>
          <div className="pipeline-grid">
            {[
              ['01', 'Parse', 'Turn the prompt and current project state into Engineering IR.'],
              ['02', 'Validate', 'Check dimensions, operations, labels, and references against the schema.'],
              ['03', 'Route', 'Send the validated IR to the Blender or OpenSCAD adapter.'],
              ['04', 'Execute', 'Build editable objects or write SCAD and export production artifacts.'],
            ].map(([number, title, description]) => (
              <article key={number}>
                <span>{number} /</span>
                <h3>{title}</h3>
                <p>{description}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="builder-section" id="tools">
          <div className="hero-heading">
            <h1>Choose how you build.</h1>
            <p>
              Design in Blender. Engineer in OpenSCAD.
              <br />
              One local companion keeps the work moving.
            </p>
          </div>
          <div className="tool-choice">
            <article>
              <small>DESIGN</small>
              <h2>Blender</h2>
              <p>Install the add-on, prompt inside Blender, and keep your scene editable.</p>
              <a className="outline-button" href="#blender">
                Set up Blender <ArrowIcon />
              </a>
              <ToolPreview tool="blender" />
            </article>
            <div className="tool-divider" aria-hidden="true">
              <span className="tool-divider-line" />
              <span className="tool-divider-label">idō</span>
              <span className="tool-divider-line" />
            </div>
            <article>
              <small>ENGINEERING</small>
              <h2>OpenSCAD</h2>
              <p>Run one command, open the watched SCAD file, and render real parts.</p>
              <a className="outline-button" href="#openscad">
                Set up OpenSCAD <ArrowIcon />
              </a>
              <ToolPreview tool="openscad" />
            </article>
          </div>
        </section>

        <section className="local-first rule-section">
          <div>
            <h2>Designed for local workflows.</h2>
            <p>idō runs on your machine, in your tools. Your files stay local. You stay in control.</p>
          </div>
          <ul>
            <li><strong>Local first</strong><span>Runs on your machine and writes ordinary project files.</span></li>
            <li><strong>Private by default</strong><span>Your local companion owns desktop access.</span></li>
            <li><strong>Built for makers and engineers</strong><span>Use the tools you already know.</span></li>
          </ul>
        </section>

        <LocalControl />

        <SetupSection
          id="blender"
          tool="blender"
          title="Design with Blender."
          description="Install the idō add-on once. Prompt, revise, and inspect Engineering IR without leaving the 3D View."
          steps={[
            'Download the Blender add-on.',
            'Blender → Edit → Preferences → Add-ons → Install from Disk.',
            'Press N and open the idō tab.',
          ]}
          command="ido open blender"
        />

        <SetupSection
          id="openscad"
          tool="openscad"
          title="Engineer with OpenSCAD."
          description="Install OpenSCAD and idō from source, then open a watched SCAD file with validated geometry and exports."
          steps={[
            'Install OpenSCAD (brew install --cask openscad on macOS — use Generate setup commands for other OSes).',
            'Clone the repo, create a venv, pip install -e ., and copy .env.example to .env.',
            'Set OPENAI_API_KEY in .env, then run ido open openscad.',
          ]}
          command="ido open openscad"
          setupGenerator
        />

        <section className="workflow rule-section" id="docs">
          <div className="workflow-heading">
            <h2>One command surface.</h2>
            <p>The same companion starts the API, opens tools, and saves project state.</p>
          </div>
          <div className="command-list">
            <CopyCommand command='ido prompt --tool blender "make a compact enclosure"' />
            <CopyCommand command='ido prompt --tool openscad "add two M4 mounting holes"' />
            <CopyCommand command="ido status" />
          </div>
        </section>

        <section className="downloads rule-section" id="download">
          <div>
            <h2>Download idō.</h2>
            <p>Choose your operating system, then install the Blender add-on or open OpenSCAD from the CLI.</p>
          </div>
          <div className="download-list">
            {downloadLinks.map(([label, href, detail]) => (
              <a href={href} key={label}>
                <strong>{label}</strong>
                <span>{detail}</span>
                <ArrowIcon />
              </a>
            ))}
            <a href={BLENDER_ADDON}>
              <strong>Blender add-on</strong>
              <span>ido_blender.zip</span>
              <ArrowIcon />
            </a>
          </div>
        </section>

        <section className="troubleshooting rule-section">
          <div>
            <h2>Troubleshooting.</h2>
            <p>Start with the companion status, then verify the desktop application is installed.</p>
          </div>
          <div>
            <details>
              <summary>The local dashboard does not open.</summary>
              <p>Run <code>ido status</code>. If needed, run <code>ido serve</code> and open http://127.0.0.1:8010.</p>
            </details>
            <details>
              <summary>Blender cannot reach the backend.</summary>
              <p>Set the add-on backend URL to http://127.0.0.1:8010 and start idō once.</p>
            </details>
            <details>
              <summary>OpenSCAD exports are missing.</summary>
              <p>Confirm the OpenSCAD CLI is installed and available on PATH. The SCAD source is still saved when export tools are unavailable.</p>
            </details>
          </div>
        </section>
      </main>

      <footer>
        <span>idō</span>
        <p>A universal local interface for modeling software.</p>
        <a href={REPOSITORY}>GitHub</a>
      </footer>
    </>
  )
}
