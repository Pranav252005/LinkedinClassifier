import { defineConfig } from "@neon/config/v1";

export default defineConfig({
  auth: true,
  preview: {
    functions: {
      api: { name: "api", source: "./hello.ts" },
    },
  },
});
