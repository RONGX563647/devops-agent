export interface Health {
  status: string;
}

export function formatHealth(health: Health): string {
  return `status=${health.status}`;
}
