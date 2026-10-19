"""Desktop front end for the local SKU image renaming workflow."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser


def load_engine():
    spec = importlib.util.spec_from_file_location('sku_engine_5381', Path(__file__).with_name('5381_图片改名.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class App:
    def __init__(self, root):
        self.root, self.engine = root, load_engine()
        self.events, self.busy, self.report = queue.Queue(), False, None
        root.title('SKU Image Rename Studio')
        root.geometry('1100x740')
        root.minsize(900, 600)
        root.configure(bg='#eef3f7')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame', background='#eef3f7')
        style.configure('TLabel', background='#eef3f7', foreground='#18364b', font=('Segoe UI', 11))
        style.configure('TButton', font=('Segoe UI', 11), padding=(12, 8))
        style.configure('Treeview', font=('Segoe UI', 10), rowheight=28)
        self.source, self.mapping = tk.StringVar(), tk.StringVar()
        self.status = tk.StringVar(value='Choose the image folder and naming CSV to begin.')
        header = tk.Frame(root, bg='#14344a', padx=24, pady=20)
        header.pack(fill='x')
        tk.Label(header, text='SKU IMAGE RENAME STUDIO', bg='#14344a', fg='#79d7ca', font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        tk.Label(header, text='Exact filenames. Verified copies.', bg='#14344a', fg='white', font=('Segoe UI', 25, 'bold')).pack(anchor='w', pady=5)
        tk.Label(header, text='Preview a mapping, preserve extension case, and verify every delivered file.', bg='#14344a', fg='#c1d4df', font=('Segoe UI', 11)).pack(anchor='w')
        body = ttk.Frame(root, padding=24)
        body.pack(fill='both', expand=True)
        for label, variable, command in [('Image folder', self.source, self.choose_folder), ('Mapping CSV', self.mapping, self.choose_csv)]:
            row = ttk.Frame(body)
            row.pack(fill='x', pady=4)
            ttk.Label(row, text=label, width=14).pack(side='left')
            ttk.Entry(row, textvariable=variable, state='readonly').pack(side='left', fill='x', expand=True, padx=8)
            ttk.Button(row, text='Choose', command=command).pack(side='right')
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=14)
        self.preview = ttk.Button(actions, text='1  Validate & preview', command=lambda: self.start(False))
        self.preview.pack(side='left')
        self.execute = ttk.Button(actions, text='2  Create verified copies', command=lambda: self.start(True))
        self.execute.pack(side='left', padx=10)
        self.open_button = ttk.Button(actions, text='Open audit report', command=self.open_report, state='disabled')
        self.open_button.pack(side='right')
        self.table = ttk.Treeview(body, columns=('original', 'new_name', 'bytes'), show='headings')
        for key, label, width in [('original','SOURCE FILENAME',420),('new_name','DELIVERY FILENAME',420),('bytes','BYTES',100)]:
            self.table.heading(key, text=label)
            self.table.column(key, width=width, anchor='w')
        self.table.pack(fill='both', expand=True)
        self.progress = ttk.Progressbar(body, mode='indeterminate')
        self.progress.pack(fill='x', pady=(14, 8))
        ttk.Label(body, textvariable=self.status, wraplength=1020).pack(anchor='w')
        root.after(80, self.poll)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def choose_folder(self):
        if not self.busy:
            value = filedialog.askdirectory(mustexist=True)
            if value: self.source.set(value)

    def choose_csv(self):
        if not self.busy:
            value = filedialog.askopenfilename(filetypes=[('CSV manifest','*.csv')])
            if value: self.mapping.set(value)

    def start(self, execute):
        if self.busy: return
        if not self.source.get() or not self.mapping.get():
            messagebox.showinfo('Choose inputs', 'Choose both the image folder and mapping CSV.')
            return
        destination = None
        if execute:
            destination = filedialog.askdirectory(title='Choose a new or empty output folder')
            if not destination: return
        self.busy = True
        self.preview.configure(state='disabled')
        self.execute.configure(state='disabled')
        self.open_button.configure(state='disabled')
        self.progress.start()
        self.status.set('Checking the complete batch and source bytes...')
        source, mapping = self.source.get(), self.mapping.get()
        def work():
            try:
                result = self.engine.execute_plan(source,mapping,destination) if execute else self.engine.load_plan(source,mapping)
                self.events.put(('ok',result,destination))
            except Exception as error:
                self.events.put(('error',str(error),None))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            kind, result, destination = self.events.get_nowait()
            self.busy = False
            self.progress.stop()
            self.preview.configure(state='normal')
            self.execute.configure(state='normal')
            if kind == 'error':
                self.status.set('Validation failed. No delivery was created.')
                messagebox.showerror('Batch needs correction',result)
            else:
                self.table.delete(*self.table.get_children())
                records = result.get('plan', result.get('files', result.get('records', [])))
                for item in records:
                    self.table.insert('', 'end', values=(item.get('original',''),item.get('new_name',''),item.get('bytes','')))
                count = result.get('counts', {}).get('files',len(records))
                self.status.set(f'{count:,} files checked. ' + ('Verified delivery created. Originals unchanged.' if destination else 'Ready. No files have been copied yet.'))
                if destination:
                    self.report = Path(destination)/'5381_运行报告.html'
                    self.open_button.configure(state='normal' if self.report.exists() else 'disabled')
        except queue.Empty:
            pass
        self.root.after(80,self.poll)

    def open_report(self):
        if self.report and self.report.exists(): webbrowser.open(self.report.resolve().as_uri())

    def close(self):
        if self.busy:
            messagebox.showinfo('Processing', 'Wait for the current batch to finish before closing.')
        else:
            self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    App(root)
    root.mainloop()
