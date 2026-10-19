/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"] },
      colors: {
        ink: { 50: "#f6f7f9", 100: "#eceef2", 200: "#d5d9e0", 400: "#8a93a3", 600: "#4b5565", 800: "#1f2633", 900: "#121722" },
        brand: { 50: "#eef4ff", 100: "#dbe7ff", 500: "#2f6bff", 600: "#1f55e6", 700: "#1a45b8" },
      },
      boxShadow: { page: "0 1px 3px rgba(16,24,40,.12), 0 8px 24px rgba(16,24,40,.08)" },
    },
  },
  plugins: [],
};
