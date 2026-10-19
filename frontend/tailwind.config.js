/** @type {import('tailwindcss').Config} */
// Tema "cidade à noite": noir, névoa e luz de holofote dourada.
// A escala "ink" é invertida (50 = fundo mais escuro, 900 = texto mais claro).
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        display: ["Cinzel", "Georgia", "serif"],
      },
      colors: {
        ink: { 50: "#0a0c11", 100: "#161b24", 200: "#283041", 400: "#7f8899", 600: "#b4bccb", 800: "#e1e5ec", 900: "#f3f4f7" },
        night: { 950: "#06070a", 900: "#0d1017", 800: "#141923", 700: "#1d2330" },
        brand: { 50: "#2a2312", 100: "#3a2f14", 500: "#f2b632", 600: "#ffc84d", 700: "#ffd979" },
      },
      boxShadow: {
        page: "0 0 0 1px rgba(242,182,50,.08), 0 18px 50px rgba(0,0,0,.65)",
        glow: "0 0 24px rgba(242,182,50,.35)",
      },
    },
  },
  plugins: [],
};
