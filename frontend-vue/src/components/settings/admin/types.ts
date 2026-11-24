import type { AgentSettingsConfig } from '@/api/admin'

export type SettingsTabKey = 'general' | 'models' | 'runtime' | 'mail' | 'data'

export interface RateLimitConfig {
  global: number
  tenant: number
  user: number
}

export interface CacheConfig {
  l1Capacity: number
  l2Ttl: number
  semanticThreshold: number
  prefetchEnabled: boolean
}

export interface ApiKeyConfig {
  circuitBreakerThreshold: number
  recoveryTimeout: number
  weightDecay: number
  autoRecovery: boolean
}

export interface QuotaConfig {
  tenant_daily_cost_cents: number
  user_daily_cost_cents: number
  device_write_max_projects: number
}

export interface ModelRateConfig {
  gateway_model_rpm: number
  gateway_model_rps: number
  gateway_model_user_rpm: number
  gateway_model_user_rps: number
  gateway_model_tpm: number
  gateway_model_user_tpm: number
  sandbox_max_concurrency_per_tenant: number
}

export interface MailConfigDraft {
  provider: 'smtp' | 'qingchen'
  host: string
  port: number
  username: string
  password: string
  security: 'none' | 'starttls' | 'ssl'
  from: string
  from_name: string
  reply_to: string
  base_url: string
  api_key: string
  timeout_seconds: number
}

export interface SettingsSectionState {
  ready: boolean
  dirty: boolean
  saving: boolean
}

export type AgentSettingsModel = AgentSettingsConfig
