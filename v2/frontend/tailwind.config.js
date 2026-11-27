/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  // Hover styles only apply on devices that really hover (no sticky hover after a tap).
  future: { hoverOnlyWhenSupported: true },
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        stage: "rgb(var(--c-stage) / <alpha-value>)",
        panel: "rgb(var(--c-panel) / <alpha-value>)",
        tile: "rgb(var(--c-tile) / <alpha-value>)",
        fg: "rgb(var(--c-fg) / <alpha-value>)",
        muted: "rgb(var(--c-muted) / <alpha-value>)",
        faint: "rgb(var(--c-faint) / <alpha-value>)",
        accent: "rgb(var(--c-accent) / <alpha-value>)",
        "accent-ink": "rgb(var(--c-accent-ink) / <alpha-value>)",
        danger: "rgb(var(--c-danger) / <alpha-value>)",
        success: "rgb(var(--c-success) / <alpha-value>)",
        warning: "rgb(var(--c-warning) / <alpha-value>)",
        line: "var(--line)",
        edge: "var(--edge)"
      },
      transitionTimingFunction: {
        out: "cubic-bezier(0.23, 1, 0.32, 1)",
        "in-out": "cubic-bezier(0.77, 0, 0.175, 1)"
      },
      boxShadow: {
        card: "var(--shadow-card)",
        pop: "var(--shadow-pop)"
      }
    }
  },
  plugins: []
};
