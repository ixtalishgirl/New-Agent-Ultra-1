// Type shim for `tsc --noEmit` (moduleResolution "bundler" resolves
// ../browser-system/node-integration/browserActions.js -> this file).

export const BROWSER_ACTIONS_LIST: string[];
export function getBrowserPermissions(): Record<string, any>;
export function redactSecrets(text: any): any;
export function wrapUntrusted(content: any): string;
export function buildObservationContext(obs: any): string;
export function executeBrowserAction(
  action: string,
  args: any,
  deps?: { legacyInspect?: (url: string) => Promise<any> }
): Promise<{ toolCall: any; observation: any }>;

declare const _default: {
  executeBrowserAction: typeof executeBrowserAction;
  getBrowserPermissions: typeof getBrowserPermissions;
  buildObservationContext: typeof buildObservationContext;
  redactSecrets: typeof redactSecrets;
  BROWSER_ACTIONS_LIST: string[];
};
export default _default;
