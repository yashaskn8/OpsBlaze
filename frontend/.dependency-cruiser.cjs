/** @type {import('dependency-cruiser').IConfiguration} */
module.exports = {
  forbidden: [
    {
      name: "no-circular",
      severity: "error",
      comment: "Prevent circular imports across frontend modules.",
      from: {},
      to: { circular: true },
    },
    {
      name: "no-orphans",
      severity: "error",
      comment: "Every TypeScript module in src must be reachable from main.tsx.",
      from: {
        orphan: true,
        pathNot: ["(^|/)\\.d\\.ts$", "(^|/)vite-env\\.d\\.ts$", "(^|/)main\\.tsx$"],
      },
      to: {},
    },
    {
      name: "leaf-helpers-stay-pure",
      severity: "error",
      comment: "Leaf API/stream/auth/guidance helpers must not import UI components or main.tsx.",
      from: {
        path: "^frontend/src/(api|auth|clientDiagnostics|incidentStream|investigationGuidance|lineagePages|workspacePaths)\\.ts$",
      },
      to: {
        path: "^frontend/src/.*\\.tsx$",
      },
    },
    {
      name: "components-do-not-import-entrypoint",
      severity: "error",
      comment: "UI subcomponents must not import main.tsx.",
      from: {
        path: "^frontend/src/(?!main\\.tsx$).*\\.tsx$",
      },
      to: {
        path: "^frontend/src/main\\.tsx$",
      },
    },
  ],
  options: {
    doNotFollow: { path: "node_modules" },
    tsConfig: { fileName: "frontend/tsconfig.json" },
    enhancedResolveOptions: {
      exportsFields: ["exports"],
      conditionNames: ["import", "require", "node", "default", "types"],
    },
  },
};
