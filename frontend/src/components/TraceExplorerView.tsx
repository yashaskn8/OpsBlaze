import { useState, useEffect } from 'react'
import type { TraceGraphResponse, TraceNode } from '../types/stage6'

interface Props {
  incidentId: string
  tenantId: string
}

export function TraceExplorerView({ incidentId, tenantId }: Props) {
  const [graph, setGraph] = useState<TraceGraphResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedNode, setSelectedNode] = useState<TraceNode | null>(null)

  const fetchTrace = async () => {
    if (!incidentId) return
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`/v1/incidents/${encodeURIComponent(incidentId)}/trace`, {
        headers: {
          'X-Tenant-ID': tenantId,
        },
      })
      if (!res.ok) {
        throw new Error(`Failed to load trace graph: HTTP ${res.status}`)
      }
      const data = (await res.json()) as TraceGraphResponse
      setGraph(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void fetchTrace()
  }, [incidentId, tenantId])

  if (loading) {
    return <div style={{ padding: '20px' }}>Loading causal trace DAG...</div>
  }

  if (error) {
    return (
      <div style={{ padding: '20px', color: '#dc2626' }}>
        <strong>Error loading trace:</strong> {error}
      </div>
    )
  }

  if (!graph) {
    return <div style={{ padding: '20px' }}>No trace data available.</div>
  }

  return (
    <div className="b1-panel" style={{ padding: '20px', background: '#ffffff', borderRadius: '8px', border: '1px solid #cbd5e1' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h3 style={{ margin: 0 }}>P3 — Evidence-to-Effect Trace Explorer</h3>
        <div style={{ display: 'flex', gap: '8px' }}>
          <span style={{ fontSize: '12px', padding: '4px 8px', background: '#e2e8f0', borderRadius: '4px' }}>
            Nodes: {graph.nodes.length} | Edges: {graph.edges.length}
          </span>
          <span
            style={{
              fontSize: '12px',
              padding: '4px 8px',
              borderRadius: '4px',
              background: graph.has_cycles ? '#fee2e2' : '#dcfce7',
              color: graph.has_cycles ? '#991b1b' : '#166534',
              fontWeight: 600,
            }}
          >
            {graph.has_cycles ? '⚠️ CYCLES DETECTED' : '✓ Strict Acyclic DAG'}
          </span>
          <button
            type="button"
            onClick={() => void fetchTrace()}
            style={{ padding: '4px 10px', fontSize: '12px', borderRadius: '4px', border: '1px solid #cbd5e1', cursor: 'pointer' }}
          >
            Refresh DAG
          </button>
        </div>
      </div>

      {graph.missing_links.length > 0 && (
        <div style={{ padding: '10px 14px', background: '#fffbeb', border: '1px solid #fef3c7', color: '#92400e', borderRadius: '6px', marginBottom: '16px' }}>
          <strong>⚠️ Trace Missing Link Warnings:</strong>
          <ul style={{ margin: '6px 0 0 16px', padding: 0 }}>
            {graph.missing_links.map((ml, idx) => (
              <li key={idx} style={{ fontSize: '12px' }}>{ml}</li>
            ))}
          </ul>
        </div>
      )}

      {/* DAG Flow Columns */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px' }}>
        {graph.nodes.map((node) => {
          const isSelected = selectedNode?.id === node.id
          const hasWarning = !!node.warning
          return (
            <div
              key={node.id}
              onClick={() => setSelectedNode(node)}
              style={{
                padding: '12px',
                borderRadius: '6px',
                border: isSelected ? '2px solid #2563eb' : hasWarning ? '1px solid #f87171' : '1px solid #e2e8f0',
                background: isSelected ? '#eff6ff' : hasWarning ? '#fff5f5' : '#f8fafc',
                cursor: 'pointer',
                boxShadow: isSelected ? '0 2px 4px rgba(37,99,235,0.1)' : 'none',
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                <span style={{ fontSize: '10px', fontWeight: 'bold', color: '#64748b', textTransform: 'uppercase' }}>
                  {node.kind}
                </span>
                <span style={{ fontSize: '11px', padding: '2px 6px', borderRadius: '10px', background: '#e2e8f0' }}>
                  {node.status}
                </span>
              </div>
              <div style={{ fontWeight: 600, fontSize: '13px', marginBottom: '4px' }}>
                {node.label}
              </div>
              <div style={{ fontSize: '10px', color: '#94a3b8' }}>
                Timestamp: {new Date(node.timestamp).toLocaleTimeString()}
              </div>
              {hasWarning && (
                <div style={{ marginTop: '6px', fontSize: '11px', color: '#dc2626', fontWeight: 500 }}>
                  ⚠️ {node.warning}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Node Details Inspector */}
      {selectedNode && (
        <div style={{ marginTop: '20px', padding: '16px', background: '#f1f5f9', borderRadius: '6px', border: '1px solid #cbd5e1' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
            <h4 style={{ margin: 0 }}>Node Inspector: <code>{selectedNode.id}</code></h4>
            <button
              type="button"
              onClick={() => setSelectedNode(null)}
              style={{ padding: '2px 8px', fontSize: '11px', border: '1px solid #cbd5e1', borderRadius: '4px', cursor: 'pointer' }}
            >
              Close
            </button>
          </div>
          <pre style={{ margin: 0, padding: '10px', background: '#ffffff', borderRadius: '4px', fontSize: '11px', overflowX: 'auto', border: '1px solid #e2e8f0' }}>
            {JSON.stringify(selectedNode.metadata, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
