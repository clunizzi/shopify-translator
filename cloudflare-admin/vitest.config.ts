import { defineConfig } from "vitest/config";

export default defineConfig({
  assetsInclude: ["**/*.html", "**/*.css", "**/*.txt"],
  test: {
    environment: "node",
  },
});
