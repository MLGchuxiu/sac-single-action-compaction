import Quartz, sys, os
from Foundation import NSURL
path = sys.argv[1]
url = NSURL.fileURLWithPath_(path)
doc = Quartz.PDFDocument.alloc().initWithURL_(url)
n = doc.pageCount()
out = []
for i in range(n):
    out.append(doc.pageAtIndex_(i).string())
txt = "\n\n".join(out)
open(sys.argv[2], "w").write(txt)
print("pages:", n, "chars:", len(txt))
