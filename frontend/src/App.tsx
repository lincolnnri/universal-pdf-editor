import { useState } from "react";
import type { DocInfo } from "./lib/api";
import { api } from "./lib/api";
import { UploadScreen } from "./components/UploadScreen";
import { Editor } from "./components/Editor";

export default function App() {
  const [doc, setDoc] = useState<DocInfo | null>(null);
  if (!doc) return <UploadScreen onLoaded={setDoc} />;
  return (
    <Editor
      key={doc.id}
      doc={doc}
      onClose={() => {
        void api.close(doc.id);
        setDoc(null);
      }}
    />
  );
}
