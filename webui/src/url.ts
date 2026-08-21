export function displayUrl(value: string): string {
  try {
    const url = new URL(value)
    const path = `${url.pathname}${url.search ? '?…' : ''}${url.hash ? '#…' : ''}`
    const compact = `${url.hostname}${path}`
    return compact.length > 96 ? `${compact.slice(0, 93)}…` : compact
  } catch {
    return value.length > 96 ? `${value.slice(0, 93)}…` : value
  }
}
