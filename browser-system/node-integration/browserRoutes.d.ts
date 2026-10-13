// Type shim for `tsc --noEmit` (moduleResolution "bundler" resolves
// ../browser-system/node-integration/browserRoutes.js -> this file).
import { Router } from 'express';

declare const router: Router;
export default router;

export function getBrowserPermissions(): Record<string, any>;
