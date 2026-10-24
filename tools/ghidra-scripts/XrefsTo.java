// Find references to a list of addresses.
// Usage: -postScript XrefsTo.java <addressListFile>   (one 0xADDR per line)
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;
import java.io.BufferedReader;
import java.io.FileReader;

public class XrefsTo extends GhidraScript {
    @Override
    public void run() throws Exception {
        try (BufferedReader br = new BufferedReader(new FileReader(getScriptArgs()[0]))) {
            String line;
            while ((line = br.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) continue;
                String[] p = line.split("\\s+", 2);
                Address a = toAddr(p[0]);
                String hint = p.length > 1 ? p[1] : "";
                println("== REFS TO " + a + " " + hint);
                ReferenceIterator it = currentProgram.getReferenceManager().getReferencesTo(a);
                int n = 0;
                while (it.hasNext()) {
                    Reference r = it.next();
                    Address from = r.getFromAddress();
                    Function f = getFunctionContaining(from);
                    println("   from " + from + " type=" + r.getReferenceType()
                        + " fn=" + (f == null ? "<none>" : f.getName() + "@" + f.getEntryPoint()));
                    n++;
                }
                if (n == 0) println("   (none)");
            }
        }
    }
}
