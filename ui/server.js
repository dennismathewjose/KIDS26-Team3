import "dotenv/config";

import express from "express";
import multer from "multer";
import cors from "cors";
import fs from "fs";

console.log("🚀 Starting UI Server...");

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static("public"));

const upload = multer({ dest: "uploads/" });
const externalApiUrl = process.env.EXTERNAL_API_URL?.trim();
const externalApiFoldersPath = process.env.EXTERNAL_API_GET_FOLDERS_CONTENT?.trim();
const externalApiUploadPath = process.env.EXTERNAL_API_UPLOAD_FILES_BATCH?.trim();
const FOLDERS_URL = externalApiUrl && externalApiFoldersPath
  ? new URL(externalApiFoldersPath, `${externalApiUrl}/`).toString()
  : null;
const UPLOAD_URL = externalApiUrl && externalApiUploadPath
  ? new URL(externalApiUploadPath, `${externalApiUrl}/`).toString()
  : null;
const externalApiField = process.env.EXTERNAL_FIELD || "files";

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

async function clearUploadsDirectory() {
  const uploadDirectory = "uploads";
  await fs.promises.mkdir(uploadDirectory, { recursive: true });
  const entries = await fs.promises.readdir(uploadDirectory, { withFileTypes: true });

  await Promise.all(
    entries
      .filter((entry) => entry.isFile())
      .map((entry) => fs.promises.unlink(`${uploadDirectory}/${entry.name}`))
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

app.get("/files", async (req, res) => {
  if (!FOLDERS_URL) {
    return res.status(503).json({
      error: "External API is not configured. Set EXTERNAL_API_URL and EXTERNAL_API_GET_FOLDERS_CONTENT.",
    });
  }

  try {
    const externalResponse = await fetch(FOLDERS_URL);
    const responseText = await externalResponse.text();
    let responseBody;

    try {
      responseBody = JSON.parse(responseText);
    } catch {
      responseBody = { response: responseText };
    }

    if (!externalResponse.ok) {
      return res.status(502).json({
        error: "External API rejected the folder content request.",
        details: responseBody,
      });
    }
    console.log(`📥 Fetched folder contents from ${FOLDERS_URL}`);
    console.log(responseBody);
    res.status(externalResponse.status).json(responseBody);
  } catch (err) {
    console.error("❌ External folders error:", err.message);
    res.status(502).json({ error: "Failed to fetch folder contents" });
  }
});

app.post("/upload", (req, res, next) => {
  upload.array("file", 20)(req, res, (error) => {
    if (error) {
      return res.status(400).json({ error: `Upload rejected: ${error.message}` });
    }
    next();
  });
}, async (req, res) => {
  const uploadedFiles = req.files || [];

  try {
    if (!uploadedFiles.length) {
      return res.status(400).json({ error: "No files uploaded" });
    }

    if (!UPLOAD_URL) {
      await removeUploadedFiles(uploadedFiles);
      return res.status(503).json({
        error: "External API is not configured. Set EXTERNAL_API_URL and EXTERNAL_API_UPLOAD_FILES_BATCH.",
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

    console.log(
      `📤 Forwarding ${uploadedFiles.length} document(s) to ${UPLOAD_URL}`
    );
    const externalResponse = await fetch(UPLOAD_URL, {
      method: "POST",
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
    console.error("❌ External upload error details:", err);
    res.status(502).json({ error: "Failed to forward docs to external API" });
  }
});

const port = process.env.PORT || 8080;

await clearUploadsDirectory();
console.log("🧹 Cleared uploads directory.");

app.listen(port, () => {
  console.log(`✅ Server running on http://localhost:${port}`);
});