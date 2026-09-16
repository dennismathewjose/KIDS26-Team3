import "dotenv/config";

import express from "express";
import multer from "multer";
import cors from "cors";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

console.log("🚀 Starting UI Server...");

const __filename = fileURLToPath(import.meta.url);


const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static("public"));

const upload = multer({ dest: "uploads/" });
const externalApiUrl = process.env.EXTERNAL_API_URL;
const externalApiKey = process.env.EXTERNAL_API_KEY;
const externalAField = process.env.EXTERNAL_FIELD || "api";

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

app.post("/upload", upload.array("api", 20), async (req, res) => {
  const uploadedFiles = req.files || [];

  try {
    if (!uploadedFiles.length) {
      return res.status(400).json({ error: "No files uploaded" });
    }

    if (!externalApiUrl) {
      await removeUploadedFiles(uploadedFiles);
      return res.status(503).json({
        error: "External API is not configured. Set EXTERNAL_API_URL.",
      });
    }

    const formData = new FormData();
    for (const file of uploadedFiles) {
      const fileBuffer = await fs.promises.readFile(file.path);
      formData.append(
        externalApiField,
        new Blob([fileBuffer], { type: file.mimetype || "application/octet-stream" }),
        file.originalname
      );
    }

    const headers = externalApiKey
      ? { Authorization: `Bearer ${externalApiKey}` }
      : undefined;

    console.log(
      `📤 Forwarding ${uploadedFiles.length} document(s) to ${externalApiUrl}`
    );
    const externalResponse = await fetch(externalApiUrl, {
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
        error: "External API rejected the upload.",
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
    res.status(502).json({ error: "Failed to forward docs to external API" });
  }
});

const port = process.env.PORT || 8080;

app.listen(port, () => {
  console.log(`✅ Server running on http://localhost:${port}`);
  console.log("📌 POST /upload  → Upload doc");
});