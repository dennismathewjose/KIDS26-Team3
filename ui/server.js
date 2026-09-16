import "dotenv/config";

import express from "express";
import multer from "multer";
import cors from "cors";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

console.log("🚀 Starting RAG PDF Server...");

const __filename = fileURLToPath(import.meta.url);


const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static("public"));

const upload = multer({ dest: "uploads/" });
const externalPdfApiUrl = process.env.EXTERNAL_PDF_API_URL;
const externalPdfApiKey = process.env.EXTERNAL_PDF_API_KEY;
const externalPdfField = process.env.EXTERNAL_PDF_FIELD || "pdf";

async function removeUploadedFiles(files) {
  await Promise.all(
    files.map(async (file) => {
      try {
        await fs.promises.unlink(file.path);
      } catch (error) {
        if (error.code !== "ENOENT") {
          console.error(`❌ Could not remove ${file.path}:`, error.message);
        }
      }
    })
  );
}

function getReceivedFiles(responseBody) {
  const candidates = [
    responseBody?.receivedFiles,
    responseBody?.files,
    responseBody?.documents,
    responseBody?.data?.files,
  ];
  const files = candidates.find(Array.isArray) || [];

  return files.map((file) => {
    if (typeof file === "string") return file;
    return file.name || file.filename || file.originalname || "Unnamed file";
  });
}

app.post("/upload", upload.array("pdf", 20), async (req, res) => {
  const uploadedFiles = req.files || [];

  try {
    if (!uploadedFiles.length) {
      return res.status(400).json({ error: "No files uploaded" });
    }

    if (!externalPdfApiUrl) {
      await removeUploadedFiles(uploadedFiles);
      return res.status(503).json({
        error: "External PDF API is not configured. Set EXTERNAL_PDF_API_URL.",
      });
    }

    const formData = new FormData();
    for (const file of uploadedFiles) {
      const fileBuffer = await fs.promises.readFile(file.path);
      formData.append(
        externalPdfField,
        new Blob([fileBuffer], { type: file.mimetype || "application/octet-stream" }),
        file.originalname
      );
    }

    const headers = externalPdfApiKey
      ? { Authorization: `Bearer ${externalPdfApiKey}` }
      : undefined;

    console.log(
      `📤 Forwarding ${uploadedFiles.length} document(s) to ${externalPdfApiUrl}`
    );
    const externalResponse = await fetch(externalPdfApiUrl, {
      method: "POST",
      headers,
      body: formData,
    });

    const responseText = await externalResponse.text();
    let responseBody;
    try {
      responseBody = JSON.parse(responseText);
    } catch {
      responseBody = { response: responseText };
    }

    if (!externalResponse.ok) {
      await removeUploadedFiles(uploadedFiles);
      return res.status(502).json({
        error: "External PDF API rejected the upload.",
        details: responseBody,
      });
    }

    await removeUploadedFiles(uploadedFiles);
    console.log(`✅ ${uploadedFiles.length} document(s) forwarded successfully!`);
    res.status(externalResponse.status).json({
      message: `✅ ${uploadedFiles.length} document(s) forwarded successfully!`,
      receivedFiles: getReceivedFiles(responseBody),
      result: responseBody,
    });

  } catch (err) {
    await removeUploadedFiles(uploadedFiles);
    console.error("❌ External upload error:", err.message);
    res.status(502).json({ error: "Failed to forward PDFs to external API" });
  }
});

const port = process.env.PORT || 8080;

app.listen(port, () => {
  console.log(`✅ Server running on http://localhost:${port}`);
  console.log("📌 POST /upload  → Upload PDF");
});