import { useAppApi } from '@kirocrew/app-sdk'
import { Card, CardTitle, PageHeader, StatCard } from '@kirocrew/app-sdk/ui'
import { useState, useEffect } from 'react'

export default function ConfigSync() {
  const api = useAppApi()
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // Fetch initial data here
    setLoading(false)
  }, [])

  return (
    <>
      <PageHeader title="Config Sync" subtitle="A Kiro Crew app: Config Sync" />
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label="Status" value="OK" accent />
        </div>
        <Card>
          <CardTitle>Overview</CardTitle>
          {loading
            ? <p className="text-sm text-muted">Loading…</p>
            : <p className="text-sm text-muted">Your app content goes here.</p>
          }
        </Card>
      </div>
    </>
  )
}
