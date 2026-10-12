type StatusNoticeProps = {
  children: React.ReactNode
  tone?: 'info' | 'success' | 'warning' | 'error'
  title?: string
  action?: React.ReactNode
}

export default function StatusNotice({
  children,
  tone = 'info',
  title,
  action,
}: StatusNoticeProps) {
  return (
    <div className={`notice notice--${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <div>
        {title && <strong>{title}</strong>}
        <p>{children}</p>
      </div>
      {action}
    </div>
  )
}
