interface Props {
  pingLagSeconds: number
  isDisconnected: boolean
}

export function StaleLockoutBanner({ pingLagSeconds, isDisconnected }: Props) {
  if (!isDisconnected && pingLagSeconds <= 30) {
    return null
  }

  return (
    <div
      className="b1-alert b1-alert-danger"
      role="alert"
      style={{
        margin: '16px 0',
        border: '2px solid #ef4444',
        backgroundColor: '#fee2e2',
        color: '#991b1b',
        padding: '12px 16px',
        borderRadius: '6px',
      }}
    >
      <strong>⚠️ CRITICAL STALE-VIEW LOCKOUT:</strong>{' '}
      {isDisconnected
        ? 'Real-time telemetry stream is disconnected.'
        : `SSE Heartbeat telemetry lag is ${pingLagSeconds.toFixed(1)}s (exceeds 30.0s threshold).`}{' '}
      All approval and consequential mutation controls are strictly locked to protect against split-brain execution on stale state.
    </div>
  )
}
