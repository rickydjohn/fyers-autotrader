import { Component, type ReactNode } from 'react'

interface Props { children: ReactNode }
interface State { hasError: boolean; message: string }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message }
  }

  componentDidCatch(error: Error) {
    console.error('[ErrorBoundary]', error)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-gray-950 flex flex-col items-center justify-center gap-4 text-gray-400">
          <p className="text-sm">Something went wrong rendering the UI.</p>
          <p className="text-xs font-mono text-gray-600">{this.state.message}</p>
          <button
            onClick={() => this.setState({ hasError: false, message: '' })}
            className="px-4 py-2 text-xs bg-gray-800 hover:bg-gray-700 rounded border border-gray-700"
          >
            Retry
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
