# CHAT_PATCH.md — rendering BrowserPanel in HalyeStudio.tsx

File under patch: `src/components/HalyeStudio.tsx` (3204 lines as of 2026-10-13).
New component: `browser-system/chat-ui/BrowserPanel.tsx` (no changes needed).

---

## HUNK 1 — import (after the existing component imports, ~line 71)

Current (~lines 64–72):
```tsx
import { ScreenshotModal } from './ScreenshotModal';
import { ActionHistoryCard } from './ActionHistoryCard';
import { FullProcessModal } from './FullProcessModal';
import { BLANK_CANVAS_CODE } from '../templates';
```

Insert after the `BLANK_CANVAS_CODE` import line:

```tsx
import { BrowserPanel } from '../../browser-system/chat-ui/BrowserPanel';
```

(If the lint/tsconfig balks at importing outside `src`, copy `BrowserPanel.tsx`
into `src/components/` instead and import from `'./BrowserPanel'`.)

---

## HUNK 2 — render the panel (after the Web Eyes block, before the ZIP card)

Current (~lines 1789–1794):
```tsx
                  </div>
                )}

                {/* ZIP Archive Inspection Card */}
                {msg.zipInspection && (
```

Insert between the `)}` closing the webInspection block and the
`{/* ZIP Archive Inspection Card */}` comment:

```tsx
                {/* Live Browser Observation — real rendered browsing (new) */}
                {msg.browserObservation && (
                  <BrowserPanel observation={msg.browserObservation} />
                )}

                {/* ZIP Archive Inspection Card */}
```

The panel handles `null`/missing fields internally, so partial observations
(e.g. failed searches with no screenshot) render safely.

---

## HUNK 3 (optional but recommended) — type the new message field

File: `src/types.ts`, interface `ChatMessage` (~line 115, after `webInspection?`):

Current:
```ts
  webInspection?: WebInspectionResult;
```

Add after it:
```ts
  browserObservation?: import('../../browser-system/chat-ui/BrowserPanel').BrowserObservation;
```

(Or `browserObservation?: any;` if you prefer to keep types.ts dependency-free.)

---

## Note for the parent: message wiring gap (pre-existing, not introduced here)

As of 2026-10-13, nothing in `HalyeStudio.tsx` populates `msg.toolCalls` /
`msg.webInspection` on chat messages: `handleSendPrompt` (~line 696) POSTs to
`/api/model/chat` (whose response is plain text), and the only
`/api/agent/generate` caller is the realtime builder (~line 961), which ignores
the extra response fields. The render blocks at ~1641 and ~1753 exist but are
currently unreachable from live chat.

For `BrowserPanel` to appear, whichever flow the parent chooses must map the
generate response onto the assistant message, e.g.:

```tsx
const assistantMessage: ChatMessage = {
  id: 'ast-' + Date.now(),
  role: 'assistant',
  text: data.text,
  timestamp: new Date().toLocaleTimeString(),
  toolCalls: data.toolCalls,
  webInspection: data.webInspection,
  browserObservation: data.browserObservation, // <-- new
};
```

Until that wiring lands, the panel code above is safe to merge — it renders
nothing when `msg.browserObservation` is absent.
