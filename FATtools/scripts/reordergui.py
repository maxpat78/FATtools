""" Reorder.pyw     V. 0.10

Visually alters a FAT/FAT32 directory table order. 

Very useful with micro Hi-Fi supporting USB keys but low of memory and bad
in browsing."""

import sys, os, argparse

if sys.version_info >= (3,0): 
    from tkinter import *
    import tkinter.messagebox as messagebox
else:
    from Tkinter import *
    import tkMessageBox as messagebox

from FATtools.Volume import vopen, vclose

DEBUG=int(os.getenv('FATTOOLS_DEBUG', '0'))

if DEBUG:
    import logging
    logging.basicConfig(level=logging.DEBUG, filename='reorder_gui.log', filemode='w')


class ReorderableListbox(Listbox):
    """ A Tkinter listbox with drag & drop reordering of lines """
    def __init__(self, master, **kw):
        kw['selectmode'] = EXTENDED
        Listbox.__init__(self, master, kw)
        self.bind('<Button-1>', self.setCurrent)
        self.bind('<Control-1>', self.toggleSelection)
        self.bind('<B1-Motion>', self.shiftSelection)
        self.bind('<Leave>',  self.onLeave)
        self.bind('<Enter>',  self.onEnter)
        self.selectionClicked = False
        self.left = False
        self.unlockShifting()
        self.ctrlClicked = False

    def orderChangedEventHandler(self): pass

    def onLeave(self, event):
        # prevents changing selection when dragging
        # already selected items beyond the edge of the listbox
        if self.selectionClicked:
            self.left = True
            return 'break'

    def onEnter(self, event):
        #TODO
        self.left = False

    def setCurrent(self, event):
        self.ctrlClicked = False
        i = self.nearest(event.y)
        self.selectionClicked = self.selection_includes(i)
        if (self.selectionClicked):
            return 'break'

    def toggleSelection(self, event):
        self.ctrlClicked = True

    def moveElement(self, source, target):
        if not self.ctrlClicked:
            element = self.get(source)
            self.delete(source)
            self.insert(target, element)

    def unlockShifting(self):
        self.shifting = False

    def lockShifting(self):
        # prevent moving processes from disturbing each other
        # and prevent scrolling too fast
        # when dragged to the top/bottom of visible area
        self.shifting = True

    def shiftSelection(self, event):
        if self.ctrlClicked:
            return
        selection = self.curselection()
        if not self.selectionClicked or len(selection) == 0:
            return

        selectionRange = range(min(selection), max(selection))
        currentIndex = self.nearest(event.y)

        if self.shifting:
            return 'break'

        lineHeight = 15
        bottomY = self.winfo_height()
        if event.y >= bottomY - lineHeight:
            self.lockShifting()
            self.see(self.nearest(bottomY - lineHeight) + 1)
            self.master.after(500, self.unlockShifting)
        if event.y <= lineHeight:
            self.lockShifting()
            self.see(self.nearest(lineHeight) - 1)
            self.master.after(500, self.unlockShifting)

        if currentIndex < min(selection):
            self.lockShifting()
            notInSelectionIndex = 0
            for i in selectionRange[::-1]:
                if not self.selection_includes(i):
                    self.moveElement(i, max(selection)-notInSelectionIndex)
                    notInSelectionIndex += 1
            currentIndex = min(selection)-1
            self.moveElement(currentIndex, currentIndex + len(selection))
            self.orderChangedEventHandler()
        elif currentIndex > max(selection):
            self.lockShifting()
            notInSelectionIndex = 0
            for i in selectionRange:
                if not self.selection_includes(i):
                    self.moveElement(i, min(selection)+notInSelectionIndex)
                    notInSelectionIndex += 1
            currentIndex = max(selection)+1
            self.moveElement(currentIndex, currentIndex - len(selection))
            self.orderChangedEventHandler()
        self.unlockShifting()
        return 'break'



class Manipulator(Tk):
    def __init__ (p):
        Tk.__init__(p)
        p.disk = '' # stores the device/image to open
        p.root = None # opened root Dirtable
        p.title("Reorder a FAT/FAT32 directory table")
        p.geometry('640x510')
        frame = Frame(p, width=640, height=480)
        scroll = Scrollbar(frame, orient=VERTICAL)
        p.list = ReorderableListbox(frame, selectmode=EXTENDED, yscrollcommand=scroll.set, width=600, height=25)
        p.list.bind('<Double-Button-1>', p.on2click)
        scroll.config(command=p.list.yview)
        scroll.pack(side=RIGHT, fill=Y)
        p.list.pack()
        frame.pack()
        frame2 = Frame(frame)
        b = Button(frame2, text="UP", command=p.move_up)
        b.pack(side=LEFT)
        b = Button(frame2, text="DN", command=p.move_down)
        b.pack(side=LEFT)
        b = Button(frame2, text="T", command=p.move_top)
        b.pack(side=LEFT)
        b = Button(frame2, text="Scan", command=p.scan)
        b.pack(side=LEFT, padx=5)
        p.scan_button = b
        b = Button(frame2, text="Apply", command=p.apply)
        b.pack(side=LEFT, padx=5)
        b = Button(frame2, text="Quit", command=p.quit)
        b.pack(side=LEFT, padx=5)
        b = Button(frame2, text="Help", command=p.help)
        b.pack(side=LEFT, padx=40)
        b = Label(frame, text="Device/image with file sytem to manipulate: ")
        b.pack()
        p.drive_to_open = StringVar(frame, value='')
        b = Entry(frame, width=80, textvariable=p.drive_to_open)
        b.bind('<Return>', lambda x: p.scan())
        b.pack()
        p.tbox1 = b
        b = Label(frame, text="Path to sort: ")
        b.pack()
        p.path_to_sort = StringVar(frame, value='')
        b = Entry(frame, width=80, textvariable=p.path_to_sort)
        b.bind('<Return>', lambda x: p.scan())
        b.pack()
        p.tbox2 = b
        frame2.pack()

    def on2click(p, evt):
        w = evt.widget
        index = int(w.curselection()[0])
        value = w.get(index)
        if value == '.': return
        #~ if DEBUG:
            #~ messagebox.showinfo('Debug', 'You selected item %d: "%s"' % (index, value))
        r = p.tbox2.get()
        print(r, value)
        if value != '..':
            r += os.sep
            p.path_to_sort.set(os.path.join(r, value))
        else:
            if r.rfind(os.sep) > -1:
                p.path_to_sort.set(r[:r.rfind(os.sep)])
            else:
                p.path_to_sort.set('')
        p.scan_button.invoke() # but we don't know if it is a directory...

    def move_up(p):
        for i in p.list.curselection():
            it = p.list.get(i)
            p.list.delete(i)
            p.list.insert(i-1, it)
            p.list.selection_set(i-1)

    def move_down(p):
        for i in p.list.curselection():
            it = p.list.get(i)
            p.list.delete(i)
            p.list.insert(i+1, it)
            p.list.selection_set(i+1)
            
    def move_top(p):
        sel = p.list.curselection()
        for i, j in zip(range(len(sel)), sel):
            it = p.list.get(j)
            p.list.delete(j)
            p.list.insert(i, it)
            
    def scan(p):
        root = p.tbox1.get()
        if not root: return
        if DEBUG:
            print ("DEBUG: opening '%s'"%root)
        if root[-1] == os.sep: root = root[:-1]
        # if device/image changed, close old Dirtable
        if p.root and (root != p.disk):
            vclose(p.root)
            p.root = None
        p.disk = root
        if not p.root:
            try:
                p.root = vopen(root, 'r+b')
            except:
                messagebox.showerror('Error', 'Could not open "%s"!' % root)
                return
        relapath = p.path_to_sort.get().replace('/','\\')
        if DEBUG:
            print ("DEBUG: internal path to access:", relapath)
        if relapath in ('', '.'):
            fold = p.root
        else:
            if relapath[0] == '\\': relapath = relapath[1:]
            fold = p.root.opendir(relapath)
        if not fold:
            messagebox.showerror('Error', "\"%s\" is not a directory!" % relapath)
            p.path_to_sort.set(relapath[:relapath.rfind(os.sep)])
            return
        p.fold = fold
        if DEBUG:
            print ("DEBUG: scanning", fold.path)
        p.list.delete(0, END)
        for it in p.fold.iterator():
            p.list.insert(END, it.Name())

    def apply(p):
        li = p.list.get(0, END)
        if not li: return
        if DEBUG:
            print ("DEBUG: captured order:", li)
        p.fold._sortby.fix = li
        p.fold.sort(p.fold._sortby)

    def quit(p):
        root.destroy()

    def help(p):
        messagebox.showinfo('Quick Help', '''To edit a directory table order in a FAT/FAT32 disk:
        
- specify the device or image file containing the filing system in the first text box below
- specify the path to edit in the second box (default is root)
- press 'Enter' or select 'Scan' to (re)scan the directory table specified and show the on-disk order in the upper list box
- select one or more items and move them up, down or to the top with the mouse or the 'UP', 'DN' and 'T' buttons (double click to enter a directory)
- press 'Apply' to write the newly ordered table back to the disk
- use 'Quit' when done''')

def create_parser(parser_create_fn=argparse.ArgumentParser,parser_create_args=None):
    help_s = """
    reordergui.py
    """
    par = parser_create_fn(*parser_create_args, usage=help_s,
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description="Displays a GUI to help ordering a directory table.")
    return par

def call(args):
    root = Manipulator()
    root.mainloop()
    
if __name__ == '__main__':
    call(None)
