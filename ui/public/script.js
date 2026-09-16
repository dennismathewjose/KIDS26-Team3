const API_URL = window.location.origin;

async function uploadPDF() {
  const fileInput = document.getElementById("pdfFile");
  const status = document.getElementById("uploadStatus");

  if (!fileInput.files.length) {
    alert("Select a DOC, DOCX, PDF, or TXT file first.");
    return;
  }

  const formData = new FormData();
  for (const file of fileInput.files) {
    formData.append("pdf", file);
  }

  status.innerText = `Uploading ${fileInput.files.length} document(s) & Processing...`;

  const response = await fetch(`${API_URL}/upload`, {
    method: "POST",
    body: formData,
  });

  const data = await response.json();
  status.innerText = data.message || data.error;
}