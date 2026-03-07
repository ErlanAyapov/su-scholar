from docxtpl import DocxTemplate

doc = DocxTemplate("template.docx")

context = {
    "object": {
        "author": "Yerlan",
        "title": "AI Research",
        "date": "2026"
    }
}

doc.render(context)
doc.save("result.docx")