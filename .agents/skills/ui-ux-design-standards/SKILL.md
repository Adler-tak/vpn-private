---
name: ui-ux-design-standards
description: >-
  Strict UI/UX design standards for Web Panels, Admin Dashboards, and Telegram Bots.
  Enforces modern glassmorphism, responsive RTL layouts, bulletproof interactive buttons,
  switches, tables, modals, and consistent Persian typography.
---

# UI/UX Design & Frontend Excellence Standards

This skill defines mandatory design, styling, and ergonomics rules for all Web Panels, Admin Dashboards, Client Portals, and Telegram Bots.
Whenever developing, refactoring, or touching any user interface component (HTML, CSS, JS, Telegram Keyboards/Messages), you MUST strictly follow these guidelines.

---

## 1. Visual Aesthetics & Design System
- **Theme:** Modern Dark Glassmorphism (`bg-slate-950`, `bg-slate-900/80`, `backdrop-blur-md`, `border-slate-800`).
- **Accent Palettes:**
  - Active / Success / Live: Emerald (`emerald-400`, `emerald-500/20`, border `emerald-500/30`) with a pulsing green dot indicator (`w-2 h-2 rounded-full bg-emerald-400 animate-pulse`).
  - Inactive / Disabled: Slate / Rose (`slate-800`, text `slate-400`, border `slate-700/60`).
  - Information / Primary: Indigo / Sky (`indigo-500`, `sky-400`).
  - Warning / Attention: Amber (`amber-400`, `amber-500/20`, border `amber-500/40`).
  - Danger / Deletion: Rose (`rose-500`, `rose-500/20`).

---

## 2. Interactive Buttons & Switches (Bulletproof Mechanics)
- **NO Broken RTL Peer Checkboxes:** Never rely on Tailwind `peer-checked:after:translate-x-full` inside RTL (`dir="rtl"`) documents without explicit RTL positioning, as it flips or breaks toggle alignment across different browsers.
- **Button-Based State Toggles (Recommended):**
  Use explicit state buttons with pulsing indicators and clear Persian labels:
  ```html
  <!-- Active state -->
  <button type="button" class="flex items-center gap-1.5 px-3 py-1 rounded-xl text-xs font-bold transition shadow-sm bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 hover:bg-emerald-500/30">
    <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
    <span>روشن (فعال)</span>
  </button>

  <!-- Inactive state -->
  <button type="button" class="flex items-center gap-1.5 px-3 py-1 rounded-xl text-xs font-bold transition shadow-sm bg-slate-800/90 text-slate-400 border border-slate-700 hover:bg-slate-700 hover:text-slate-200">
    <span class="w-2 h-2 rounded-full bg-slate-500"></span>
    <span>خاموش (غیرفعال)</span>
  </button>
  ```
- **Button Ergonomics:**
  - Always pair icons with descriptive labels (e.g. `<i class="bi bi-people-fill"></i><span>مشاهده کلاینت‌ها</span>`).
  - Minimum touch/click target height: `py-1.5` to `py-2` (at least 32px to 40px).
  - Clear loading states: disable button and replace icon with `<i class="bi bi-arrow-repeat animate-spin"></i>` during network requests.

---

## 3. Cards, Inbounds, and Servers Layout
- **Header:** Flag, Bold Title, Subtitle / Code in monospace, followed by the Status Toggle Button at the top-right.
- **Body:** Key-value pairs cleanly separated by borders or subtle background chips (`bg-slate-950/60`, rounded-xl).
- **Numeric & Technical Data:** Always wrap IPs, ports, UUIDs, IDs, bandwidth, and traffic in `font-mono text-slate-200` with `dir="ltr"`.
- **Progress Bars:** Fully rounded capsules (`rounded-full h-1.5 bg-slate-800 overflow-hidden`) with smooth gradient fills (`from-emerald-500 to-cyan-400` or `from-rose-500 to-amber-500` for high load).
- **Secondary Actions:** Place secondary buttons (e.g. "مشاهده کلاینت‌های فعال") prominently beneath the relevant metrics with clear icon cues.

---

## 4. Modals and Data Inspection Views
- **Backdrop:** `fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-sm flex items-center justify-center p-4`.
- **Modal Container:** Max width `max-w-2xl` to `max-w-4xl`, rounded-2xl, glass card border `border-slate-800`, subtle outer shadow `shadow-2xl`.
- **Modal Header:** Title with icon, active/total counters, and a close button (`<i class="bi bi-x-lg"></i>`).
- **Search Bar:** Real-time client-side filter input for rapid searching.
- **Data Table:** Clean striped or divided rows, padded cells (`p-3`), status badges, and one-click copy buttons for tokens/emails.
- **Empty State:** Friendly graphic/icon with helpful Persian explanation if no data exists.

---

## 5. Persian (RTL) Typography & Micro-copy
- Proper spacing and clean Persian phrasing (avoid literal English translations).
- Clear, polite, and informative toasts (`showToast('پیام موفقیت', 'success')`).
