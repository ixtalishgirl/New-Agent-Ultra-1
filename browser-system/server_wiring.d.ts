export interface BrowserIntent {
  action: string;
  args: Record<string, any>;
}
export interface BrowserChatResult {
  intent: BrowserIntent | null;
  toolCall?: any;
  observation?: any;
  observationContext?: string;
  serviceMode?: string;
  serviceError?: string | null;
  error?: string;
}
export function detectBrowserIntent(prompt: string): BrowserIntent | null;
export function runBrowserForChat(
  prompt: string,
  deps?: { legacyInspect?: (url: string) => Promise<any> }
): Promise<BrowserChatResult>;
export function formatBrowserOnlyReply(browser: BrowserChatResult): string;
export function mountBrowserSystem(
  app: any,
  deps?: { legacyInspect?: (url: string) => Promise<any> }
): { detectBrowserIntent: typeof detectBrowserIntent; runBrowserForChat: typeof runBrowserForChat };
export function checkInfraAction(kind: string, body?: any): { ok: boolean; reason?: string };
export function checkShellAction(kind: string, opts?: any): { ok: boolean; reason?: string; audited?: boolean; nonOverridable?: boolean; permanent?: boolean };
export function checkTargetScope(target: string): { ok: boolean; reason?: string; host?: string; matched?: string };
export function checkDestructiveConfirmed(body: any): { ok: boolean; needsConfirmation?: boolean; reason?: string };
export function auditSensitiveAction(action: string, details?: any): string;
declare const _default: {
  mountBrowserSystem: typeof mountBrowserSystem;
  detectBrowserIntent: typeof detectBrowserIntent;
  runBrowserForChat: typeof runBrowserForChat;
  checkInfraAction: typeof checkInfraAction;
  checkShellAction: typeof checkShellAction;
  checkTargetScope: typeof checkTargetScope;
  checkDestructiveConfirmed: typeof checkDestructiveConfirmed;
  auditSensitiveAction: typeof auditSensitiveAction;
};
export default _default;
