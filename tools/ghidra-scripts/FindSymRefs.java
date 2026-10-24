// Find symbols whose demangled name contains a substring, then list references to them.
// Usage: -postScript FindSymRefs.java <substring>
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;
import ghidra.program.model.symbol.SymbolTable;

public class FindSymRefs extends GhidraScript {
    @Override
    public void run() throws Exception {
        String needle = getScriptArgs()[0];
        SymbolTable st = currentProgram.getSymbolTable();
        SymbolIterator it = st.getAllSymbols(true);
        int found = 0;
        while (it.hasNext()) {
            Symbol s = it.next();
            String n = s.getName(true);
            if (n.contains(needle)) {
                found++;
                Address a = s.getAddress();
                println("SYM " + a + " " + n);
                ReferenceIterator refs = currentProgram.getReferenceManager().getReferencesTo(a);
                while (refs.hasNext()) {
                    Reference r = refs.next();
                    Address from = r.getFromAddress();
                    Function f = getFunctionContaining(from);
                    println("   REF from " + from + " (" + r.getReferenceType() + ") in "
                        + (f == null ? "<none>" : f.getName() + "@" + f.getEntryPoint()));
                }
            }
        }
        println("FOUND " + found);
    }
}
